"""Segment-based collection engine.

The upstream endpoint returns a flat event list for a date range and caps each
response at ``PAGE_SIZE`` (20) records.  A response that is not full proves the
range is exhausted, so every day in that range can be marked complete -- even
days with zero events.  A full response is ambiguous, so the range is split in
half and each half is queried again.  A single day that still returns a full
page cannot be resolved through the interface and becomes ``blocked`` until an
operator uploads the complete CSV for that day.

Retries only touch days that are ``pending`` or ``failed``; days already
``complete`` or ``blocked`` are never re-requested.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .catalog import (
    MAX_SPAN_DAYS,
    STATUS_FAILED,
    STATUS_PENDING,
    CatalogSource,
    Event,
    SourceError,
    Store,
)


@dataclass
class CollectSummary:
    completed: int = 0
    blocked: int = 0
    failed: int = 0


def contiguous_segments(days: list[dt.date]) -> list[list[dt.date]]:
    segments: list[list[dt.date]] = []
    for day in sorted(days):
        if segments and day == segments[-1][-1] + dt.timedelta(days=1):
            segments[-1].append(day)
        else:
            segments.append([day])
    return segments


def collect(store: Store, source: CatalogSource, run_id: int) -> CollectSummary:
    """Query every day that is neither complete nor blocked."""
    rows = store.days(run_id)
    if not rows:
        raise KeyError(run_id)
    targets = [
        dt.date.fromisoformat(row["day"])
        for row in rows
        if row["status"] in (STATUS_PENDING, STATUS_FAILED)
    ]
    summary = CollectSummary()
    for segment in contiguous_segments(targets):
        for start in range(0, len(segment), MAX_SPAN_DAYS):
            _walk(store, source, run_id, segment[start : start + MAX_SPAN_DAYS], summary)
    return summary


def _mark_failed(store: Store, run_id: int, days: list[dt.date], message: str) -> None:
    for day in days:
        store.mark(run_id, day.isoformat(), STATUS_FAILED, last_error=message)


def _walk(
    store: Store,
    source: CatalogSource,
    run_id: int,
    days: list[dt.date],
    summary: CollectSummary,
) -> None:
    if not days:
        return
    start, end = days[0], days[-1]
    try:
        events = source.fetch_page(start, end)
    except SourceError as exc:
        _mark_failed(store, run_id, days, f"请求失败: {exc}")
        summary.failed += len(days)
        return

    page_size = source.page_size()
    if len(events) > page_size:
        _mark_failed(store, run_id, days, "接口返回条数超过单页上限")
        summary.failed += len(days)
        return

    # The source promises events inside the requested window; treat a breach as
    # a failed request rather than silently misfiling records.
    for event in events:
        try:
            event_day = dt.date.fromisoformat(event.utc_time[:10])
        except ValueError:
            event_day = None
        if event_day is None or not (start <= event_day <= end):
            _mark_failed(store, run_id, days, "接口返回了区间外的事件")
            summary.failed += len(days)
            return

    if len(events) >= page_size:
        if len(days) == 1:
            try:
                store.record_blocked_day(run_id, days[0].isoformat(), events)
            except ValueError as exc:
                _mark_failed(store, run_id, days, str(exc))
                summary.failed += 1
            else:
                summary.blocked += 1
        else:
            middle = len(days) // 2
            _walk(store, source, run_id, days[:middle], summary)
            _walk(store, source, run_id, days[middle:], summary)
        return

    # Fewer than a full page: the whole range is exhausted.
    by_day: dict[str, list[Event]] = {day.isoformat(): [] for day in days}
    for event in events:
        by_day[event.utc_time[:10]].append(event)
    try:
        store.complete_days(run_id, by_day)
    except ValueError as exc:
        _mark_failed(store, run_id, days, str(exc))
        summary.failed += len(days)
    else:
        summary.completed += len(days)
