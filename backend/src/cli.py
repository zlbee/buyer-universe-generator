"""Command line helpers for local development and smoke checks."""

from __future__ import annotations

import argparse
import json
from typing import Sequence

from src.config import get_settings
from src.repositories.database import init_db


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bug", description="Buyer Universe Generator CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="Initialize the local SQLite database")
    subparsers.add_parser("show-config", help="Print effective non-secret configuration")

    args = parser.parse_args(argv)
    settings = get_settings()

    if args.command == "init-db":
        init_db(settings)
        print(json.dumps({"status": "ok", "database_url": settings.database_url}, indent=2))
        return 0

    if args.command == "show-config":
        print(settings.model_dump_json(indent=2, exclude={"openrouter_api_key"}))
        return 0

    parser.error(f"Unsupported command: {args.command}")
    return 2
