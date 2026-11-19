"""Prompt versioning, the Langfuse source with its local fallback, and prompt sync."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_loop_and_limits import FakeProvider, SPEC

from estatequery import observability
from estatequery.agent import prompts
from estatequery.agent.loop import run_turn
from estatequery.agent.providers.base import StepDone, Usage
from estatequery.config import get_settings


def fake_prompt(text: str, version: int = 7, is_fallback: bool = False) -> SimpleNamespace:
    return SimpleNamespace(prompt=text, version=version, is_fallback=is_fallback)


@pytest.fixture()
def langfuse_source(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(get_settings(), "prompt_source", "langfuse")


def test_the_version_is_a_content_hash_that_changes_with_the_text() -> None:
    assert prompts.prompt_version() == f"local-{prompts.template_hash(prompts.TEMPLATE)}"
    assert prompts.template_hash("a") == prompts.template_hash("a") != prompts.template_hash("b")
    assert len(prompts.template_hash(prompts.TEMPLATE)) == 8


def test_variables_convert_to_and_from_langfuse_syntax() -> None:
    converted = prompts.to_langfuse(prompts.TEMPLATE)
    for name in prompts.VARIABLES:
        assert "{{" + name + "}}" in converted
    import re

    assert re.search(r"(?<!\{)\{\w+\}(?!\})", converted) is None       # no single-brace variable is left
    assert prompts.from_langfuse(converted) == prompts.TEMPLATE


def test_the_local_prompt_is_the_default_and_fills_in_the_data_facts(db_path: Path) -> None:
    text, info = prompts.system_prompt_and_info(db_path)
    assert info.source == "local" and info.version == prompts.prompt_version() and info.client is None
    assert "7 listings" in text and "2026-11-19" in text
    assert "{" not in text and "}" not in text


def test_a_langfuse_version_is_used_when_it_renders(monkeypatch, langfuse_source, db_path: Path) -> None:
    edited = prompts.to_langfuse(prompts.TEMPLATE) + "\nAlways greet the user."
    monkeypatch.setattr(observability, "fetch_prompt", lambda *a, **k: fake_prompt(edited, version=7))
    text, info = prompts.system_prompt_and_info(db_path)
    assert text.endswith("Always greet the user.") and "7 listings" in text
    assert info.source == "langfuse" and info.version.startswith("langfuse-v7-") and info.client is not None


@pytest.mark.parametrize("fetched", [
    None,                                                               # Langfuse off or unreachable
    fake_prompt("anything", is_fallback=True),                          # the SDK handed back our fallback
    fake_prompt("You are a bot. Stray {braces} here."),                 # does not fill in
    fake_prompt("Unknown {{variable}} here."),
])
def test_any_problem_with_the_langfuse_prompt_falls_back_to_the_local_one(monkeypatch, langfuse_source, db_path, fetched) -> None:
    monkeypatch.setattr(observability, "fetch_prompt", lambda *a, **k: fetched)
    text, info = prompts.system_prompt_and_info(db_path)
    assert info.source == "local" and text == prompts.system_prompt_and_info(db_path)[0]
    assert text.startswith("You are EstateQuery")


def test_a_fetch_that_raises_is_handled_inside_observability(monkeypatch) -> None:
    class Broken:
        def get_prompt(self, *a, **k):
            raise ConnectionError("down")

    monkeypatch.setattr(observability, "_langfuse", lambda: Broken())
    assert observability.fetch_prompt("p", "production", "fallback") is None
    monkeypatch.setattr(observability, "_langfuse", lambda: None)
    assert observability.fetch_prompt("p", "production", "fallback") is None


def test_the_turn_records_the_prompt_version(db_path: Path) -> None:
    async def go():
        provider = FakeProvider([StepDone(text="Hi.", usage=Usage(1, 1))])
        return [e async for e in run_turn(spec=SPEC, history=[], user_message="hi", db_path=db_path, provider=provider)]

    done = asyncio.run(go())[-1]
    assert done["prompt_version"] == prompts.prompt_version()


def test_the_system_prompt_sent_to_the_model_is_the_resolved_one(monkeypatch, langfuse_source, db_path) -> None:
    edited = prompts.to_langfuse(prompts.TEMPLATE) + "\nMARKER-FROM-LANGFUSE"
    monkeypatch.setattr(observability, "fetch_prompt", lambda *a, **k: fake_prompt(edited, version=3))
    sent: dict = {}

    class Spy(FakeProvider):
        def start(self, **kwargs):
            sent.update(kwargs)
            return super().start(**kwargs)

    async def go():
        provider = Spy([StepDone(text="Hi.", usage=Usage(1, 1))])
        return [e async for e in run_turn(spec=SPEC, history=[], user_message="hi", db_path=db_path, provider=provider)]

    done = asyncio.run(go())[-1]
    assert "MARKER-FROM-LANGFUSE" in sent["system"] and done["prompt_version"].startswith("langfuse-v3-")


# ---------------------------------------------------------------------------- sync

def test_sync_creates_a_version_only_when_the_text_changed(monkeypatch) -> None:
    pushed: list = []
    labelled: list = []
    monkeypatch.setattr(observability, "push_prompt", lambda name, text, labels, msg: pushed.append((name, text, labels)) or 4)
    monkeypatch.setattr(observability, "label_prompt", lambda name, version, labels: labelled.append((name, version, labels)))

    monkeypatch.setattr(observability, "latest_prompt", lambda name: None)
    assert "created version 4" in prompts.sync_prompt(git_sha="abc1234")
    assert pushed[0][1] == prompts.to_langfuse(prompts.TEMPLATE) and pushed[0][2] == ["git-abc1234"]

    monkeypatch.setattr(observability, "latest_prompt", lambda name: ("an older text", 4))
    assert "created version" in prompts.sync_prompt(production=True)
    assert pushed[1][2] == ["production"]

    pushed.clear()
    monkeypatch.setattr(observability, "latest_prompt", lambda name: (prompts.to_langfuse(prompts.TEMPLATE), 5))
    assert "unchanged at version 5" in prompts.sync_prompt()
    assert pushed == [] and labelled == []
    assert "labelled production" in prompts.sync_prompt(production=True)
    assert labelled == [(get_settings().prompt_name, 5, ["production"])] and pushed == []


def test_sync_without_langfuse_keys_says_so(monkeypatch) -> None:
    monkeypatch.setattr(observability, "_langfuse", lambda: None)
    with pytest.raises(RuntimeError, match="not configured"):
        prompts.sync_prompt()
