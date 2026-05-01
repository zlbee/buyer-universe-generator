"""Command line helpers for local development and smoke checks."""

from __future__ import annotations

import argparse
import json
from typing import Sequence

from src.config import get_settings
from src.pipelines.factory import build_source_ingestion_service
from src.pipelines.target_resolution import AmbiguousTargetError, TargetNotFoundError
from src.repositories.database import create_session_factory, init_db


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bug", description="Buyer Universe Generator CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="Initialize the local SQLite database")
    subparsers.add_parser("show-config", help="Print effective non-secret configuration")
    resolve_parser = subparsers.add_parser("resolve-target", help="Resolve and ingest source metadata for a target")
    resolve_parser.add_argument("query", help="Ticker, CIK, or exact company name")

    args = parser.parse_args(argv)
    settings = get_settings()

    if args.command == "init-db":
        init_db(settings)
        print(json.dumps({"status": "ok", "database_url": settings.database_url}, indent=2))
        return 0

    if args.command == "show-config":
        print(settings.model_dump_json(indent=2, exclude={"openrouter_api_key", "polygon_api_key", "news_api_key"}))
        return 0

    if args.command == "resolve-target":
        engine = init_db(settings)
        session_factory = create_session_factory(engine)
        with session_factory() as session:
            service = build_source_ingestion_service(settings, session)
            try:
                result = service.ingest(args.query)
            except AmbiguousTargetError as error:
                print(
                    json.dumps(
                        {
                            "status": "ambiguous",
                            "message": str(error),
                            "candidates": [candidate.model_dump(mode="json") for candidate in error.candidates],
                        },
                        indent=2,
                    )
                )
                return 2
            except TargetNotFoundError as error:
                print(json.dumps({"status": "not_found", "message": str(error)}, indent=2))
                return 1

        print(result.model_dump_json(indent=2))
        return 0

    parser.error(f"Unsupported command: {args.command}")
    return 2
