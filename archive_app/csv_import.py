"""Validation for full-day CSV backfills of blocked days."""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass

from .catalog import Event, canonical_time


ID_NAMES = {"event_id", "eventid", "id", "编号", "事件编号"}
TIME_NAMES = {"utc_time", "time", "utc", "datetime", "date_time", "发震时刻"}
MAG_NAMES = {"magnitude", "mag", "m", "震级"}


class CsvValidationError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class ParsedCsv:
    events: list[Event]


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CsvValidationError(["文件编码无法识别，请使用 UTF-8"])


def _column_map(header: list[str]) -> dict[str, int] | None:
    names = [cell.strip().lower() for cell in header]
    mapping: dict[str, int] = {}
    for key, aliases in (("id", ID_NAMES), ("time", TIME_NAMES), ("mag", MAG_NAMES)):
        for index, name in enumerate(names):
            if name in aliases:
                mapping[key] = index
                break
    if len(mapping) != 3:
        return None
    return mapping


def _looks_like_header(row: list[str]) -> bool:
    if len(row) < 3:
        return False
    try:
        canonical_time(row[1])
        float(row[2])
    except ValueError:
        return True
    return False


def parse_csv(data: bytes, day: str) -> ParsedCsv:
    """Parse and validate a day CSV, returning canonical events.

    Rules:
      * every record's UTC issuance time falls on ``day``;
      * event identifiers are unique within the file;
      * column order is id, utc_time, magnitude unless a header names them.
    """
    text = _decode(data)
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise CsvValidationError(["CSV 文件为空"])

    mapping = None
    if _looks_like_header(rows[0]):
        mapping = _column_map(rows[0])
        if mapping is None:
            raise CsvValidationError(
                ["表头无法识别，应包含事件编号、UTC 发震时刻、震级三列"]
            )
        rows = rows[1:]
        if not rows:
            raise CsvValidationError(["CSV 文件没有数据行"])
    else:
        mapping = {"id": 0, "time": 1, "mag": 2}

    events: list[Event] = []
    errors: list[str] = []
    seen: set[str] = set()
    for line_no, row in enumerate(rows, start=2 if mapping else 1):
        if len(row) < 3:
            errors.append(f"第 {line_no} 行缺少列")
            continue
        event_id = row[mapping["id"]].strip()
        time_text = row[mapping["time"]].strip()
        mag_text = row[mapping["mag"]].strip()
        if not event_id:
            errors.append(f"第 {line_no} 行事件编号为空")
            continue
        if event_id in seen:
            errors.append(f"第 {line_no} 行事件编号重复: {event_id}")
            continue
        try:
            utc_time = canonical_time(time_text)
        except ValueError:
            errors.append(f"第 {line_no} 行 UTC 发震时刻无效: {time_text!r}")
            continue
        try:
            magnitude = float(mag_text)
            if not math.isfinite(magnitude):
                raise ValueError
        except ValueError:
            errors.append(f"第 {line_no} 行震级无效: {mag_text!r}")
            continue
        if utc_time[:10] != day:
            errors.append(
                f"第 {line_no} 行不属于 {day}（UTC {utc_time}），文件只能包含当天事件"
            )
            continue
        seen.add(event_id)
        events.append(Event(event_id=event_id, utc_time=utc_time, magnitude=magnitude))

    if errors:
        raise CsvValidationError(errors)
    return ParsedCsv(events=events)


def verify_includes_collected(parsed: ParsedCsv, existing: list[Event]) -> None:
    """Every already-collected record must appear verbatim in the upload."""
    by_id = {event.event_id: event for event in parsed.events}
    missing: list[str] = []
    changed: list[str] = []
    for old in existing:
        new = by_id.get(old.event_id)
        if new is None:
            missing.append(old.event_id)
        elif new.utc_time != old.utc_time or not _same_magnitude(new.magnitude, old.magnitude):
            changed.append(old.event_id)
    errors: list[str] = []
    if missing:
        errors.append("已采记录未原样列入: " + ", ".join(sorted(missing)))
    if changed:
        errors.append("已采记录内容被修改: " + ", ".join(sorted(changed)))
    if errors:
        raise CsvValidationError(errors)


def _same_magnitude(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=0.0, abs_tol=1e-9)
