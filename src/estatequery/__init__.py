"""EstateQuery: an agentic real-estate assistant over DarGlobal and Wasalt data."""

from __future__ import annotations

import sys

COMMANDS = {
    "serve": "Run the web app (API + chat UI).",
    "build-db": "Build the SQLite database from data/raw/*.jsonl.",
    "scrape-wasalt": "Sample and scrape public Wasalt listings.",
    "scrape-darglobal": "Scrape DarGlobal project, partner and company pages.",
    "enrich": "Extract structured project facts from DarGlobal pages with Claude.",
    "eval": "Run the evaluation set against one or more models.",
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
    elif command == "scrape-darglobal":
        from estatequery.scrape.darglobal import main as darglobal_main

        darglobal_main(rest)
    elif command == "enrich":
        from estatequery.ingest.enrich import main as enrich_main

        enrich_main(rest)
    elif command == "eval":
        from estatequery.evals.run import main as eval_main

        eval_main(rest)
