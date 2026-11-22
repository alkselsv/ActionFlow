from __future__ import annotations

import argparse
import asyncio
import json
import sys

from fastapi.encoders import jsonable_encoder
from temporalio.client import Client
from temporalio.service import RPCError

from actionflow.config import Settings
from actionflow.operations import run_diagnostics, service_diagnostics
from actionflow.persistence.database import database


async def temporal_status(settings: Settings) -> dict:
    try:
        client = await asyncio.wait_for(
            Client.connect(
                settings.temporal_address, namespace=settings.temporal_namespace
            ),
            timeout=3,
        )
        healthy = await asyncio.wait_for(
            client.service_client.check_health(), timeout=3
        )
        return {
            "available": bool(healthy),
            "address": settings.temporal_address,
            "namespace": settings.temporal_namespace,
        }
    except (RPCError, OSError, TimeoutError, RuntimeError) as failure:
        return {
            "available": False,
            "address": settings.temporal_address,
            "namespace": settings.temporal_namespace,
            "reason": type(failure).__name__,
        }


async def doctor(settings: Settings, probe_provider: bool):
    engine, sessions = database(settings.database_url)
    try:
        with sessions() as db:
            result = service_diagnostics(
                db, settings, probe_provider=probe_provider
            )
        result["temporal"] = await temporal_status(settings)
        return result
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="actionflow.ops",
        description="Diagnose the local ActionFlow environment without printing conversation text.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    doctor_parser = commands.add_parser(
        "doctor", help="Check database, worker, Temporal and provider configuration"
    )
    doctor_parser.add_argument(
        "--probe-provider",
        action="store_true",
        help="Probe the external provider without sending conversation text",
    )

    run_parser = commands.add_parser(
        "run", help="Show run state and error by run_id"
    )
    run_parser.add_argument("run_id")
    run_parser.add_argument(
        "--probe-provider",
        action="store_true",
        help="Probe the external provider without sending conversation text",
    )

    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if args.command == "doctor":
        result = asyncio.run(doctor(settings, args.probe_provider))
        print(json.dumps(jsonable_encoder(result), ensure_ascii=False, indent=2))
        healthy = (
            result["database"]["available"]
            and result["temporal"]["available"]
            and result["worker"]["available"]
        )
        return 0 if healthy else 1

    engine, sessions = database(settings.database_url)
    try:
        with sessions() as db:
            result = run_diagnostics(
                db,
                args.run_id,
                settings,
                probe_provider=args.probe_provider,
            )
    finally:
        engine.dispose()
    if result is None:
        print(f"Run {args.run_id} was not found", file=sys.stderr)
        return 2
    print(json.dumps(jsonable_encoder(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
