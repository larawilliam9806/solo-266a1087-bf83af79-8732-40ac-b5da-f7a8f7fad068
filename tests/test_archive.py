import datetime as dt
import io
import json

from archive_app import create_app
from archive_app.catalog import (
    MAX_SPAN_DAYS,
    STATUS_BLOCKED,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_PENDING,
    Event,
    MockCatalogSource,
    Store,
)
from archive_app.collector import collect, contiguous_segments
from archive_app.csv_import import CsvValidationError, parse_csv, verify_includes_collected


def write_catalog(tmp_path, events):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"events": events}), encoding="utf-8")
    return str(path)


def ev(eid, day, hh=0, mm=0, mag=3.0):
    return {"id": eid, "time": f"{day}T{hh:02d}:{mm:02d}:00Z", "magnitude": mag}


def make_store(tmp_path):
    store = Store(str(tmp_path / "archive.db"))
    store.init()
    return store


def statuses(store, run_id):
    return {row["day"]: row["status"] for row in store.days(run_id)}


def test_contiguous_segments():
    d = dt.date(2024, 1, 1)
    segs = contiguous_segments([d, d + dt.timedelta(days=1), d + dt.timedelta(days=5)])
    assert len(segs) == 2
    assert len(segs[0]) == 2 and len(segs[1]) == 1


def test_under_full_range_marks_every_day_complete_including_empty(tmp_path):
    path = write_catalog(tmp_path, [ev("A", "2024-01-02"), ev("B", "2024-01-03")])
    store = make_store(tmp_path)
    run = store.create_run(dt.date(2024, 1, 1), dt.date(2024, 1, 5))
    summary = collect(store, MockCatalogSource(path), run)
    assert summary.completed == 5
    assert all(v == STATUS_COMPLETE for v in statuses(store, run).values())
    assert [r["event_id"] for r in store.events(run)] == ["A", "B"]


def test_full_page_splits_until_exhausted(tmp_path):
    # 25 events inside a 3-day range: the whole range is full (20), splitting
    # yields a 1-day + 2-day pair that are both under the limit.
    events = [ev(f"D1-{i}", "2024-01-01", i % 24, i) for i in range(8)]
    events += [ev(f"D2-{i}", "2024-01-02", i % 24, i) for i in range(9)]
    events += [ev(f"D3-{i}", "2024-01-03", i % 24, i) for i in range(8)]
    path = write_catalog(tmp_path, events)
    store = make_store(tmp_path)
    run = store.create_run(dt.date(2024, 1, 1), dt.date(2024, 1, 3))
    summary = collect(store, MockCatalogSource(path), run)
    assert summary.blocked == 0
    assert all(v == STATUS_COMPLETE for v in statuses(store, run).values())
    assert len(store.events(run)) == 25


def test_single_full_day_is_blocked_and_keeps_first_page(tmp_path):
    events = [ev(f"X{i:02d}", "2024-01-02", i % 24, i) for i in range(27)]
    path = write_catalog(tmp_path, events)
    store = make_store(tmp_path)
    run = store.create_run(dt.date(2024, 1, 1), dt.date(2024, 1, 3))
    collect(store, MockCatalogSource(path), run)
    st = statuses(store, run)
    assert st["2024-01-02"] == STATUS_BLOCKED
    assert st["2024-01-01"] == STATUS_COMPLETE
    assert st["2024-01-03"] == STATUS_COMPLETE
    assert len(store.events(run, "2024-01-02")) == 20


def test_failed_request_marks_days_and_retry_skips_complete_and_blocked(tmp_path):
    events = [ev("A", "2024-01-01")]
    events += [ev(f"X{i:02d}", "2024-01-02", i % 24, i) for i in range(25)]
    path = write_catalog(tmp_path, events)
    store = make_store(tmp_path)
    run = store.create_run(dt.date(2024, 1, 1), dt.date(2024, 1, 3))

    # The upstream rejects the whole window; none of its days can be considered
    # archived, so every non-complete day is marked for retry.
    summary = collect(store, MockCatalogSource(path, fail_dates=["2024-01-03"]), run)
    assert summary.failed == 3
    assert set(statuses(store, run).values()) == {STATUS_FAILED}

    # Retry: the full range is split, day 1/3 complete and day 2 is blocked.
    summary = collect(store, MockCatalogSource(path), run)
    assert summary.failed == 0
    st = statuses(store, run)
    assert st["2024-01-01"] == STATUS_COMPLETE
    assert st["2024-01-02"] == STATUS_BLOCKED
    assert st["2024-01-03"] == STATUS_COMPLETE

    # A further retry must not request anything: complete and blocked days are
    # skipped, and the blocked day keeps its first-page snapshot.
    before = [r["event_id"] for r in store.events(run, "2024-01-02")]
    calls = []

    class Recording(MockCatalogSource):
        def fetch_page(self, start, end):
            calls.append((start, end))
            return super().fetch_page(start, end)

    summary = collect(store, Recording(path), run)
    assert calls == []
    assert summary.completed == summary.blocked == summary.failed == 0
    assert before == [r["event_id"] for r in store.events(run, "2024-01-02")]


def test_retry_is_noop_when_everything_done(tmp_path):
    path = write_catalog(tmp_path, [ev("A", "2024-01-01")])
    store = make_store(tmp_path)
    run = store.create_run(dt.date(2024, 1, 1), dt.date(2024, 1, 1))
    collect(store, MockCatalogSource(path), run)
    calls = []

    class Recording(MockCatalogSource):
        def fetch_page(self, start, end):
            calls.append((start, end))
            return super().fetch_page(start, end)

    collect(store, Recording(path), run)
    assert calls == []


def test_run_rejects_more_than_max_span():
    assert MAX_SPAN_DAYS == 31
    # App-level enforcement is covered through the Flask test client below.


def test_csv_parse_accepts_header_and_canonicalises(tmp_path):
    data = (
        "event_id,utc_time,magnitude\n"
        "E1,2024-02-01T05:06:07Z,3.4\n"
        "E2,2024-02-01 23:59:59,1.2\n"
    ).encode()
    parsed = parse_csv(data, "2024-02-01")
    assert [e.event_id for e in parsed.events] == ["E1", "E2"]
    assert parsed.events[0].utc_time == "2024-02-01 05:06:07"


def test_csv_rejects_other_day_duplicates_and_bad_values():
    data = (
        "event_id,utc_time,magnitude\n"
        "E1,2024-02-01T05:06:07Z,3.4\n"
        "E1,2024-02-01T06:00:00Z,2.0\n"
        "E2,2024-02-02T00:00:00Z,1.0\n"
        "E3,2024-02-01T00:00:00Z,not-a-number\n"
    ).encode()
    try:
        parse_csv(data, "2024-02-01")
    except CsvValidationError as exc:
        text = " ".join(exc.errors)
        assert "重复" in text
        assert "不属于" in text
        assert "震级无效" in text
    else:  # pragma: no cover
        raise AssertionError("expected validation failure")


def test_csv_without_header_uses_positional_columns():
    parsed = parse_csv(b"E1,2024-02-01T01:02:03Z,4.1\n", "2024-02-01")
    assert parsed.events[0].event_id == "E1"


def test_verify_includes_collected_requires_verbatim_rows():
    parsed = parse_csv(b"E1,2024-02-01T01:00:00Z,3.0\n", "2024-02-01")
    verify_includes_collected(parsed, [Event("E1", "2024-02-01 01:00:00", 3.0)])
    try:
        verify_includes_collected(parsed, [Event("E1", "2024-02-01 01:00:00", 4.0)])
    except CsvValidationError:
        pass
    else:  # pragma: no cover
        raise AssertionError("magnitude change must be rejected")


# ---------------------------------------------------------------------------
# Flask flows
# ---------------------------------------------------------------------------


def make_client(tmp_path, events, fail_dates=()):
    path = write_catalog(tmp_path, events)
    app = create_app(
        database=str(tmp_path / "web.db"),
        source=MockCatalogSource(path, fail_dates=fail_dates),
        testing=True,
    )
    return app.test_client(), app


def upload(client, run_id, day, content, filename="day.csv"):
    return client.post(
        f"/runs/{run_id}/days/{day}/upload",
        data={"csv": (io.BytesIO(content.encode("utf-8")), filename)},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def test_web_end_to_end_blocked_day_backfill_and_download(tmp_path):
    events = [ev("Q", "2024-01-01")]
    events += [ev(f"X{i:02d}", "2024-01-02", i % 24, i) for i in range(23)]
    client, _ = make_client(tmp_path, events)

    resp = client.post(
        "/runs", data={"start_date": "2024-01-01", "end_date": "2024-01-03"}
    )
    assert resp.status_code == 302
    run_id = 1

    client.post(f"/runs/{run_id}/collect")
    page = client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "待补档" in page
    assert "下载完整目录" in page

    # Download is blocked until the blocked day is backfilled.
    blocked = client.get(f"/runs/{run_id}/download")
    assert blocked.status_code == 302

    rows = "event_id,utc_time,magnitude\n"
    for i in range(23):
        rows += f"X{i:02d},2024-01-02T{i % 24:02d}:{i:02d}:00Z,3.0\n"

    resp = upload(client, run_id, "2024-01-02", rows)
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert "已补档" in page
    assert "下载完整目录" in page

    out = client.get(f"/runs/{run_id}/download")
    assert out.status_code == 200
    text = out.get_data(as_text=True)
    lines = text.strip().splitlines()
    assert lines[0] == "event_id,utc_time,magnitude"
    assert len(lines) == 1 + 24  # 1 event on day 1 + 23 backfilled


def test_web_invalid_upload_preserves_progress(tmp_path):
    events = [ev(f"X{i:02d}", "2024-01-02", i % 24, i) for i in range(22)]
    client, app = make_client(tmp_path, events)
    client.post("/runs", data={"start_date": "2024-01-01", "end_date": "2024-01-02"})
    client.post("/runs/1/collect")
    store = app.extensions["store"]
    before = [dict(r) for r in store.events(1, "2024-01-02")]
    assert len(before) == 20

    # Missing an already-collected event -> rejected, progress untouched.
    bad = "event_id,utc_time,magnitude\nX00,2024-01-02T00:00:00Z,3.0\n"
    resp = upload(client, 1, "2024-01-02", bad)
    assert "校验失败" in resp.get_data(as_text=True)
    assert [dict(r) for r in store.events(1, "2024-01-02")] == before
    assert statuses(store, 1)["2024-01-02"] == STATUS_BLOCKED


def test_web_rejects_over_31_days_and_non_pending_upload(tmp_path):
    client, _ = make_client(tmp_path, [])
    resp = client.post(
        "/runs", data={"start_date": "2024-01-01", "end_date": "2024-02-01"}
    )
    assert resp.status_code == 302
    assert b"/runs/" not in resp.headers["Location"].encode()
    assert "最多 31" in client.get("/").get_data(as_text=True)

    client.post("/runs", data={"start_date": "2024-01-01", "end_date": "2024-01-01"})
    client.post("/runs/1/collect")
    resp = upload(client, 1, "2024-01-01", "E1,2024-01-01T00:00:00Z,1.0\n")
    assert "无需上传" in resp.get_data(as_text=True)


def test_web_failed_day_retry_flow(tmp_path):
    events = [ev("A", "2024-01-01")]
    path = write_catalog(tmp_path, events)
    app = create_app(
        database=str(tmp_path / "web.db"),
        source=MockCatalogSource(path, fail_dates=["2024-01-02"]),
        testing=True,
    )
    client = app.test_client()
    client.post("/runs", data={"start_date": "2024-01-01", "end_date": "2024-01-02"})
    client.post("/runs/1/collect")
    page = client.get("/runs/1").get_data(as_text=True)
    assert "待重试" in page

    app.extensions["source"] = MockCatalogSource(path)  # upstream recovered
    client.post("/runs/1/collect")
    assert statuses(app.extensions["store"], 1)["2024-01-02"] == STATUS_COMPLETE
    assert client.get("/runs/1/download").status_code == 200


def test_web_events_listing_shows_required_fields(tmp_path):
    client, _ = make_client(tmp_path, [ev("Z1", "2024-01-01", 3, 4, 2.5)])
    client.post("/runs", data={"start_date": "2024-01-01", "end_date": "2024-01-01"})
    client.post("/runs/1/collect")
    page = client.get("/runs/1").get_data(as_text=True)
    assert "Z1" in page
    assert "2024-01-01 03:04:00" in page
    assert "2.5" in page
