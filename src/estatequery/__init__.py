"""EstateQuery: an agentic real-estate assistant over DarGlobal and Wasalt data."""

from __future__ import annotations

import sys

COMMANDS = {
    "serve": "Run the web app (API + chat UI).",
    "build-db": "Build the SQLite database from data/raw/*.jsonl.",
    "scrape-wasalt": "Sample and scrape public Wasalt listings.",
    "scrape-darglobal": "Scrape DarGlobal project, partner and company pages.",
    "wasalt-urls": "Build a page of Wasalt listing links to open by hand (no automated visits).",
    "import-wasalt": "Import listings saved by the Wasalt collector userscript.",
    "enrich": "Extract structured project facts from DarGlobal pages with Claude.",
    "eval": "Run the evaluation set against one or more models.",
    "sync-prompt": "Copy the system prompt template into Langfuse prompt management as a new version.",
    "eval-retrieval": "Compare BM25, dense, hybrid and reranked passage retrieval (needs --extra retrieval).",
}


def main() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help") or argv[0] not in COMMANDS:
        print("usage: estatequery <command> [options]\n")
        for name, help_text in COMMANDS.items():
            print(f"  {name:<14} {help_text}")
        raise SystemExit(0 if argv and argv[0] in ("-h", "--help") else 1)

    command, rest = argv[0], argv[1:]
    if command == "serve":
        import argparse

        import uvicorn

        parser = argparse.ArgumentParser(prog="estatequery serve")
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=8000)
        args = parser.parse_args(rest)
        # One worker: rate limits and budget caps are kept in process memory.
        uvicorn.run("estatequery.api.app:app", host=args.host, port=args.port, workers=1,
                    proxy_headers=True, forwarded_allow_ips="*", log_level="info")
    elif command == "build-db":
        from estatequery.ingest.build_db import main as build_main

        build_main(rest)
    elif command == "scrape-wasalt":
        from estatequery.scrape.wasalt import main as wasalt_main

        wasalt_main(rest)
    elif command == "wasalt-urls":
        from estatequery.scrape.wasalt_manual import urls_main

        urls_main(rest)
    elif command == "import-wasalt":
        from estatequery.scrape.wasalt_manual import import_main

        import_main(rest)
    elif command == "scrape-darglobal":
        from estatequery.scrape.darglobal import main as darglobal_main

        darglobal_main(rest)
    elif command == "enrich":
        from estatequery.ingest.enrich import main as enrich_main

        enrich_main(rest)
    elif command == "eval":
        from estatequery.evals.run import main as eval_main

        eval_main(rest)
    elif command == "eval-retrieval":
        from estatequery.evals.retrieval import main as retrieval_main

        retrieval_main(rest)
    elif command == "sync-prompt":
        import argparse
        import subprocess

        from estatequery.agent.prompts import sync_prompt

        parser = argparse.ArgumentParser(prog="estatequery sync-prompt")
        parser.add_argument("--production", action="store_true",
                            help="also label this version `production`, which PROMPT_SOURCE=langfuse serves")
        args = parser.parse_args(rest)
        try:
            sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                                 check=True).stdout.strip() or None
        except (OSError, subprocess.CalledProcessError):
            sha = None
        try:
            print(sync_prompt(production=args.production, git_sha=sha))
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
