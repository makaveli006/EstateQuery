"""Output guard: the last check between the model's text and the user.

Two things are removed from an answer, because a model can be talked into producing them by
text it read in a tool result or by the user:

  links       only http(s) links to wasalt.sa, darglobal.co.uk, or a host that a tool returned in
              this turn survive; any other link keeps its label and loses its address
  personal    phone numbers and email addresses, except values a DarGlobal tool result contained
              in this turn (the company's own public contact details stay answerable)

`sanitize` is a pure function of the text and a `GuardPolicy`. `StreamGuard` applies the same
rule to text as it streams: it releases only whole words and keeps back anything that could
still turn into a link, an email address or a phone number. Feeding text in any chunking gives
exactly the same result as sanitizing it whole, which a test checks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

LINK_REMOVED = "[link removed]"
PHONE_REMOVED = "[phone removed]"
EMAIL_REMOVED = "[email removed]"

# One pass with alternatives, so a number inside a URL is never mistaken for a phone number.
TOKEN_RE = re.compile(
    r"""
    (?P<md>!?\[(?P<label>[^\]\n]{0,300})\]\((?P<md_url>(?:[^()\s]|\([^()\s]*\))*)(?:\s+"[^"\n]*")?\))
  | (?P<url>(?:https?://|www\.)[^\s<>"'\[\](){}]+)
  | (?P<email>[\w.+-]+@[\w-]+(?:\.[\w-]+)+)
  | (?P<phone>(?<![\w+])(?:\+|00)\d{1,3}[\s().-]*\d(?:[\s().-]*\d){6,12}(?!\d)
            |(?<![\w])05\d(?:[\s.-]?\d){7}(?!\d))
    """,
    re.VERBOSE,
)
TRAILING_PUNCTUATION = ".,;:!?"

# What could still become a multi-word construct at the end of streamed text.
MAX_PENDING = 900
NEWLINE = chr(10)
WHITESPACE = (" ", chr(10), chr(9), chr(13))
MARKDOWN_RE = re.compile(r"""!?\[[^\]\n]{0,300}\]\((?:[^()\s]|\([^()\s]*\))*(?:\s+"[^"\n]*")?\)""")
PENDING_PHONE_RE = re.compile(r"(?<![\w])(?:\+|00|05)[\d\s().-]{0,40}$")
CONTACT_TOOLS = {"search_darglobal", "get_darglobal_project", "list_darglobal_projects"}


def digits_key(value: str) -> str:
    """Phone numbers compare by their last nine digits, so +966 55 123 4567 equals 0551234567."""
    return re.sub(r"\D", "", value)[-9:]


@dataclass
class GuardPolicy:
    allowed_domains: tuple[str, ...] = ("wasalt.sa", "darglobal.co.uk")
    trusted_hosts: set[str] = field(default_factory=set)      # hosts of URLs the tools returned this turn
    trusted_phones: set[str] = field(default_factory=set)     # digits_key values from DarGlobal results
    trusted_emails: set[str] = field(default_factory=set)

    def trust_url(self, url: str) -> None:
        host = urlparse(url).hostname
        if host:
            self.trusted_hosts.add(host.lower())

    def trust_result(self, tool: str, result: dict) -> None:
        """Remember the contact details that a DarGlobal tool result itself contained."""
        if tool not in CONTACT_TOOLS:
            return
        text = json.dumps(result, ensure_ascii=False)
        for match in TOKEN_RE.finditer(text):
            if match.group("email"):
                self.trusted_emails.add(match.group("email").lower())
            elif match.group("phone"):
                self.trusted_phones.add(digits_key(match.group("phone")))

    def link_allowed(self, url: str) -> bool:
        parsed = urlparse(url.strip())
        if parsed.scheme not in ("http", "https") and not url.lower().startswith("www."):
            return False
        host = (urlparse(url if "//" in url else "https://" + url).hostname or "").lower()
        return bool(host) and any(host == d or host.endswith("." + d)
                                  for d in (*self.allowed_domains, *self.trusted_hosts))


def _clean(text: str, policy: GuardPolicy, removed: list[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        if match.group("md"):
            label = _clean(match.group("label"), policy, removed)
            if policy.link_allowed(match.group("md_url")):
                # An allowed address can still sit behind a label that shows a bad one.
                return match.group(0).replace(match.group("label"), label, 1)
            removed.append("link")
            return label.strip() or LINK_REMOVED
        if match.group("url"):
            url = match.group("url")
            core = url.rstrip(TRAILING_PUNCTUATION)
            tail = url[len(core):]
            if policy.link_allowed(core):
                return url
            removed.append("link")
            return LINK_REMOVED + tail
        if match.group("email"):
            if match.group("email").lower() in policy.trusted_emails:
                return match.group(0)
            removed.append("email")
            return EMAIL_REMOVED
        if digits_key(match.group("phone")) in policy.trusted_phones:
            return match.group(0)
        removed.append("phone")
        return PHONE_REMOVED

    return TOKEN_RE.sub(replace, text)


def sanitize(text: str, policy: GuardPolicy) -> tuple[str, list[str]]:
    """The text with disallowed links, phone numbers and emails removed, and what was removed."""
    removed: list[str] = []
    return _clean(text, policy, removed), removed


class StreamGuard:
    """Sanitizes streamed text, releasing only what can no longer change."""

    def __init__(self, policy: GuardPolicy) -> None:
        self.policy = policy
        self.buffer = ""

    def feed(self, text: str) -> str:
        self.buffer += text
        cut = self._safe_end()
        if cut <= 0:
            return ""
        released, self.buffer = self.buffer[:cut], self.buffer[cut:]
        return sanitize(released, self.policy)[0]

    def flush(self) -> str:
        released, self.buffer = self.buffer, ""
        return sanitize(released, self.policy)[0]

    def _safe_end(self) -> int:
        """Index up to which the buffer is final: before the word being typed, before a link,
        phone number or email that has started but not finished, and never inside a complete one."""
        text = self.buffer
        cut = max(text.rfind(ch) for ch in WHITESPACE) + 1   # start of the last word
        pending = _pending_markdown_start(text)
        if pending is not None:
            cut = min(cut, pending)
        phone = PENDING_PHONE_RE.search(text)
        if phone:
            cut = min(cut, phone.start())
        for match in TOKEN_RE.finditer(text):       # a complete link or phone number is released whole
            if match.start() < cut < match.end():
                cut = match.start()
        return cut


def _pending_markdown_start(text: str) -> int | None:
    """Where a markdown link that is still being written begins, or None.

    A link is pending while its label is open, while it waits for "(", and while its address is
    unfinished. Plain brackets such as "[1] note" are not links and are not held.
    """
    start = text.rfind("[")
    if start < 0 or start < len(text) - MAX_PENDING:
        return None
    if start > 0 and text[start - 1] == "!":
        start -= 1
    segment = text[start:]
    if NEWLINE in segment:
        return None
    close = segment.find("]")
    if close < 0 or close == len(segment) - 1:
        return start
    if segment[close + 1] != "(":
        return None
    return None if MARKDOWN_RE.match(segment) else start
