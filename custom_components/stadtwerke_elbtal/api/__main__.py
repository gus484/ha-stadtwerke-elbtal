"""CLI: print meter readings as a table.

Run it as the top-level ``api`` package from the integration directory:

    cd custom_components/stadtwerke_elbtal
    python -m api --user X

``python -m custom_components.stadtwerke_elbtal.api`` does not work without Home
Assistant installed: it imports the integration's ``__init__.py`` first, and
that imports Home Assistant. The client itself needs only aiohttp.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
from datetime import date, datetime

import aiohttp

from .client import DEFAULT_HOST, PortalClient
from .exceptions import PortalError
from .models import Reading


def _parse_date(text: str) -> date:
    return datetime.strptime(text, "%Y-%m-%d").date()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stadtwerke_elbtal",
        description="Read electricity meter readings from the Stadtwerke Elbtal "
        "customer portal.",
    )
    parser.add_argument("--user", required=True, help="portal login (email address)")
    parser.add_argument(
        "--password",
        help="portal password (prompted for if omitted)",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="portal base URL")
    parser.add_argument("--start", type=_parse_date, help="earliest date (YYYY-MM-DD)")
    parser.add_argument("--end", type=_parse_date, help="latest date (YYYY-MM-DD)")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="enable debug logging"
    )
    return parser


def _print_table(readings: list[Reading]) -> None:
    if not readings:
        print("no readings found")
        return
    date_w = max(len("Datum"), 10)
    val_w = max(len("Zählerstand"), *(len(f"{r.value}") for r in readings))
    print(f"{'Datum':<{date_w}}  {'Zählerstand':>{val_w}}  Einheit  Register    Quelle")
    print(f"{'-' * date_w}  {'-' * val_w}  -------  ----------  ------")
    for r in sorted(readings, key=lambda x: x.reading_date, reverse=True):
        print(
            f"{r.reading_date.isoformat():<{date_w}}  "
            f"{str(r.value):>{val_w}}  {r.unit:<7}  {r.register or '':<10}  "
            f"{r.source.value}"
        )


async def _run(args: argparse.Namespace) -> int:
    password = args.password or getpass.getpass("Portal password: ")
    # The client never owns a session; in Home Assistant it is handed one,
    # here the CLI opens and closes its own.
    async with aiohttp.ClientSession() as session:
        client = PortalClient(args.user, password, session, host=args.host)
        try:
            await client.login()
            contracts = await client.get_contracts()
            meters, _ = await client.get_meters_and_recent(contracts)
            for meter in meters:
                print(f"\nZähler {meter.meter_no} ({meter.medium}, {meter.unit})")
                readings = await client.get_readings(meter)
                if args.start is not None:
                    readings = [r for r in readings if r.reading_date >= args.start]
                if args.end is not None:
                    readings = [r for r in readings if r.reading_date <= args.end]
                _print_table(readings)
        except PortalError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
