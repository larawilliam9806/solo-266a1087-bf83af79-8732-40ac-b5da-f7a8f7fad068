"""Cross-task validation for compiling a monthly catalog version.

A version may draw each day from a different archive task.  Event identifiers
are only unique *within* a task, so two source tasks can legitimately disagree
about who owns an identifier.  Such a clash makes the compiled catalog
ambiguous and must reject the whole version rather than silently dropping or
renaming a row.
"""

from __future__ import annotations

import sqlite3


class PublishError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def find_duplicate_events(
    conn: sqlite3.Connection, sources: dict[str, int]
) -> dict[str, list[tuple[str, int]]]:
    """Map event ID -> every (day, source run) occurrence across the selection.

    Only identifiers that appear on two or more *different* days are reported;
    within one run the events table already enforces uniqueness.
    """
    seen: dict[str, list[tuple[str, int]]] = {}
    for day in sorted(sources):
        run_id = sources[day]
        for row in conn.execute(
            "SELECT event_id FROM events WHERE run_id=? AND day=? ORDER BY event_id",
            (run_id, day),
        ):
            seen.setdefault(row["event_id"], []).append((day, run_id))
    return {
        event_id: occurrences
        for event_id, occurrences in seen.items()
        if len({day for day, _ in occurrences}) > 1
    }


def duplicate_report(
    duplicates: dict[str, list[tuple[str, int]]]
) -> list[str]:
    """Human-readable lines naming the ID, each date and each source task."""
    errors = []
    for event_id in sorted(duplicates):
        places = "，".join(
            f"{day}（任务 #{run_id}）" for day, run_id in duplicates[event_id]
        )
        errors.append(f"事件编号 {event_id} 出现在多个日期: {places}")
    return errors


def verify_publish(
    conn: sqlite3.Connection,
    sources: dict[str, int],
    _day_rows=None,
) -> None:
    """Callback for ``Store.publish_version``; raises before anything is written."""
    duplicates = find_duplicate_events(conn, sources)
    if duplicates:
        raise PublishError(duplicate_report(duplicates))
