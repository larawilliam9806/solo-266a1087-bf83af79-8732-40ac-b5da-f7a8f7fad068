#!/usr/bin/env python3
"""Export one day from the simulated catalog as a backfill CSV.

Handy for demoing the "待补档" upload flow: the generated file satisfies the
validation rules (only that UTC day, unique IDs, all API-collected rows kept
verbatim) because it is the full day straight from the source.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("day", help="UTC date, YYYY-MM-DD")
    parser.add_argument("--catalog", default="seed/catalog.json")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    day = dt.date.fromisoformat(args.day).isoformat()
    with open(args.catalog, encoding="utf-8") as fh:
        events = json.load(fh)["events"]
    rows = sorted(
        (e for e in events if e["time"][:10] == day),
        key=lambda e: (e["time"], e["id"]),
    )
    out = Path(args.out or f"seed/backfill_{day}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["event_id", "utc_time", "magnitude"])
        for event in rows:
            stamp = dt.datetime.strptime(event["time"], "%Y-%m-%dT%H:%M:%SZ")
            writer.writerow(
                [
                    event["id"],
                    stamp.strftime("%Y-%m-%d %H:%M:%S"),
                    f"{event['magnitude']:g}",
                ]
            )
    print(f"wrote {len(rows)} events for {day} to {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
