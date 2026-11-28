"""Check tournament directory table parity and measure full-page HTML requests."""

from argparse import ArgumentParser
import json
import os
from pathlib import Path

from bs4 import BeautifulSoup

from compare import Case, benchmark, request, servers


def table_rows(server, case):
    status, _, body = request(server, case)
    if status != 200:
        raise AssertionError(f"{server.name} {case.path} returned {status}")
    document = BeautifulSoup(body, "html.parser")
    return [
        [" ".join(cell.get_text().split()) for cell in row.find_all(["th", "td"])]
        for row in document.select("table tr")
    ]


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--per-page", type=int, default=260)
    parser.add_argument("--serial-requests", type=int, default=30)
    parser.add_argument("--concurrent-requests", type=int, default=120)
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()
    if not (
        1 <= args.per_page <= 1000
        and args.serial_requests > 0
        and 1 <= args.concurrency <= args.concurrent_requests
    ):
        parser.error("Use a supported page size and positive request counts covering concurrency")

    database = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    case = Case(f"/core/tournaments/?per_page={args.per_page}&sort=-date", accept="text/html")
    results = []
    with servers(database, cold=True) as (django, rust):
        expected, candidate = table_rows(django, case), table_rows(rust, case)
        if len(expected) < 2 or expected != candidate:
            raise AssertionError(f"Tournament table differs ({len(expected)} vs {len(candidate)} rows)")
        for count, concurrency in ((args.serial_requests, 1), (args.concurrent_requests, args.concurrency)):
            for server in (django, rust):
                results.append({"server": server.name, **benchmark(server, case, count, concurrency)})
    print(json.dumps({"path": case.path, "tournaments": len(expected) - 1, "results": results}, indent=2))


if __name__ == "__main__":
    main()
