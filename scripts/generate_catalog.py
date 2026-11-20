#!/usr/bin/env python3
"""Generate a deterministic demo catalog for the local source.

The seed is designed to exercise every collection path:

* quiet stretches (zero events -> days still count as complete),
* ordinary sparse days,
* a day with exactly 20 events (a full page -> blocked by contract),
* a dense day with 27 events (blocked until a CSV backfill is uploaded).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
from pathlib import Path

START = dt.date(2024, 3, 1)
DENSE_DAY = dt.date(2024, 3, 12)
EXACT_PAGE_DAY = dt.date(2024, 3, 21)
QUIET = {dt.date(2024, 3, 4), dt.date(2024, 3, 5), dt.date(2024, 3, 6)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", nargs="?", default="seed/catalog.json")
    args = parser.parse_args()

    rng = random.Random(20240301)
    events = []
    serial = 0
    for offset in range(61):
        day = START + dt.timedelta(days=offset)
        if day in QUIET:
            continue
        if day == DENSE_DAY:
            count = 27
        elif day == EXACT_PAGE_DAY:
            count = 20
        else:
            count = rng.choice([0, 0, 1, 2, 3, 5, 8])
        for _ in range(count):
            serial += 1
            second = rng.randrange(0, 24 * 3600)
            stamp = dt.datetime.combine(day, dt.time(), tzinfo=dt.timezone.utc) + dt.timedelta(
                seconds=second
            )
            events.append(
                {
                    "id": f"LG{serial:05d}",
                    "time": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "magnitude": round(rng.uniform(0.4, 5.6), 1),
                }
            )
    events.sort(key=lambda e: (e["time"], e["id"]))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"source": "地方台网模拟目录", "events": events}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(events)} events to {out}")


if __name__ == "__main__":
    main()
