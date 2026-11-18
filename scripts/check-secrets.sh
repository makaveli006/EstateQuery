#!/bin/sh
# Blocks API keys and .env files from reaching the public repository.
#
#   sh scripts/check-secrets.sh staged   check what is about to be committed (pre-commit hook)
#   sh scripts/check-secrets.sh tree     check every tracked file (CI)

mode="${1:-staged}"

# Anthropic, OpenRouter, Langfuse (public and secret) and AWS access key IDs.
pattern='sk-ant-[A-Za-z0-9_-]{20,}|sk-or-v1-[A-Za-z0-9]{32,}|pk-lf-[A-Za-z0-9-]{20,}|sk-lf-[A-Za-z0-9-]{20,}|AKIA[0-9A-Z]{16}'

case "$mode" in
  staged)
    files=$(git diff --cached --name-only --diff-filter=ACM)
    content=$(git diff --cached -U0 --diff-filter=ACM | grep '^+' | grep -v '^+++')
    ;;
  tree)
    files=$(git ls-files)
    content=$(git grep -I -h -E -e "$pattern" -- . 2>/dev/null)
    ;;
  *)
    echo "usage: check-secrets.sh staged|tree" >&2
    exit 2
    ;;
esac

status=0

env_files=$(printf '%s\n' "$files" | grep -E '(^|/)\.env($|\.)' | grep -v '\.env\.example$')
if [ -n "$env_files" ]; then
  echo "BLOCKED: environment files must not be committed:" >&2
  printf '  %s\n' $env_files >&2
  status=1
fi

hits=$(printf '%s\n' "$content" | grep -E -o -e "$pattern" | cut -c1-12 | sort -u)
if [ -n "$hits" ]; then
  echo "BLOCKED: text that looks like an API key (first characters shown):" >&2
  printf '  %s...\n' $hits >&2
  status=1
fi

if [ "$status" -ne 0 ]; then
  echo "Remove the secret, and rotate the key if it was ever pushed." >&2
fi
exit $status
