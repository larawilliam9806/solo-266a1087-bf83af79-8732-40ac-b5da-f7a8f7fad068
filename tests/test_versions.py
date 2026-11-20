"""Tests for compiling monthly versions from multiple archive tasks."""

import datetime as dt
import json

from archive_app import create_app
from archive_app.catalog import (
    STATUS_BLOCKED,
    STATUS_COMPLETE,
    Event,
    MockCatalogSource,
    Store,
)


def ev(eid, day, hh=0, mm=0, mag=3.0):
    return {"id": eid, "time": f"{day}T{hh:02d}:{mm:02d}:00Z", "magnitude": mag}


def write_catalog(tmp_path, events, name="catalog.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"events": events}), encoding="utf-8")
    return str(path)


def make_app(tmp_path, events, fail_dates=(), name="catalog.json", db="web.db"):
    path = write_catalog(tmp_path, events, name=name)
    app = create_app(
        database=str(tmp_path / db),
        source=MockCatalogSource(path, fail_dates=fail_dates),
        testing=True,
    )
    return app


def create_and_collect(client, start, end):
    client.post("/runs", data={"start_date": start, "end_date": end})
    runs = client.get("/").get_data(as_text=True)
    # The most recently created run has the highest id.
    run_id = max(int(part.split('"')[0]) for part in runs.split("/runs/")[1:] if part[:1].isdigit())
    client.post(f"/runs/{run_id}/collect")
    return run_id


def publish(client, start, end, source_by_day):
    data = {"start_date": start, "end_date": end, "day": list(source_by_day)}
    for day, run_id in source_by_day.items():
        data[f"source_{day}"] = str(run_id)
    return client.post("/versions", data=data, follow_redirects=True)


def version_ids(app):
    store: Store = app.extensions["store"]
    return [row["id"] for row in store.versions()]


def test_publish_combines_days_from_two_tasks(tmp_path):
    events_a = [ev("A1", "2024-01-01"), ev("A2", "2024-01-02")]
    events_b = [ev("B3", "2024-01-03")]
    app = make_app(tmp_path, events_a)
    client = app.test_client()

    run_a = create_and_collect(client, "2024-01-01", "2024-01-02")
    app.extensions["source"] = MockCatalogSource(write_catalog(tmp_path, events_b, "b.json"))
    run_b = create_and_collect(client, "2024-01-03", "2024-01-03")
    assert run_a != run_b

    resp = publish(
        client,
        "2024-01-01",
        "2024-01-03",
        {"2024-01-01": run_a, "2024-01-02": run_a, "2024-01-03": run_b},
    )
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert f"任务 #{run_a}" in page
    assert f"任务 #{run_b}" in page
    assert "A1" in page and "B3" in page

    store = app.extensions["store"]
    versions = store.versions()
    assert len(versions) == 1
    version = versions[0]
    assert version["day_count"] == 3
    assert version["event_count"] == 3
    days = store.version_days(version["id"])
    assert [d["run_id"] for d in days] == [run_a, run_a, run_b]
    assert [d["event_count"] for d in days] == [1, 1, 1]

    csv_resp = client.get(f"/versions/{version['id']}/download")
    assert csv_resp.status_code == 200
    lines = csv_resp.get_data(as_text=True).strip().splitlines()
    assert lines[0] == "event_id,utc_time,magnitude"
    assert len(lines) == 4


def test_publish_rejects_incomplete_source_day(tmp_path):
    path = write_catalog(tmp_path, [ev(f"X{i:02d}", "2024-01-02", i % 24, i) for i in range(22)])
    app = create_app(
        database=str(tmp_path / "web.db"),
        source=MockCatalogSource(path),
        testing=True,
    )
    client = app.test_client()
    run = create_and_collect(client, "2024-01-01", "2024-01-02")
    store = app.extensions["store"]
    assert {r["day"]: r["status"] for r in store.days(run)}["2024-01-02"] == STATUS_BLOCKED

    resp = publish(client, "2024-01-01", "2024-01-02", {"2024-01-01": run, "2024-01-02": run})
    page = resp.get_data(as_text=True)
    assert "尚未查全" in page
    assert store.versions() == []
    # Progress is untouched by the rejected publication.
    assert len(store.events(run, "2024-01-02")) == 20


def test_publish_rejects_duplicate_id_across_dates_and_lists_details(tmp_path):
    app = make_app(tmp_path, [ev("DUP", "2024-01-01", 1)])
    client = app.test_client()
    run_a = create_and_collect(client, "2024-01-01", "2024-01-01")

    app.extensions["source"] = MockCatalogSource(
        write_catalog(tmp_path, [ev("DUP", "2024-01-02", 2)], "b.json")
    )
    run_b = create_and_collect(client, "2024-01-02", "2024-01-02")

    resp = publish(
        client,
        "2024-01-01",
        "2024-01-02",
        {"2024-01-01": run_a, "2024-01-02": run_b},
    )
    page = resp.get_data(as_text=True)
    assert "事件编号 DUP" in page
    assert "2024-01-01" in page and "2024-01-02" in page
    assert f"任务 #{run_a}" in page and f"任务 #{run_b}" in page

    store = app.extensions["store"]
    assert store.versions() == []
    # Both tasks keep their progress and events.
    assert [r["event_id"] for r in store.events(run_a)] == ["DUP"]
    assert [r["event_id"] for r in store.events(run_b)] == ["DUP"]


def test_zero_event_day_publishes_normally(tmp_path):
    app = make_app(tmp_path, [])
    client = app.test_client()
    run = create_and_collect(client, "2024-02-01", "2024-02-01")
    store = app.extensions["store"]
    assert {r["day"]: r["status"] for r in store.days(run)}["2024-02-01"] == STATUS_COMPLETE

    resp = publish(client, "2024-02-01", "2024-02-01", {"2024-02-01": run})
    assert "版本 v" in resp.get_data(as_text=True)
    version = store.versions()[0]
    assert version["event_count"] == 0
    assert store.version_days(version["id"])[0]["event_count"] == 0
    csv_text = client.get(f"/versions/{version['id']}/download").get_data(as_text=True)
    assert csv_text.strip() == "event_id,utc_time,magnitude"


def test_cleanup_requires_every_day_published_and_preserves_on_failure(tmp_path):
    events = [ev("A", "2024-03-01", 1), ev("B", "2024-03-02", 2), ev("C", "2024-03-03", 3)]
    app = make_app(tmp_path, events)
    client = app.test_client()
    run = create_and_collect(client, "2024-03-01", "2024-03-03")
    store = app.extensions["store"]

    # Publish only the first two days: cleanup must be refused.
    publish(client, "2024-03-01", "2024-03-02", {"2024-03-01": run, "2024-03-02": run})
    resp = client.post(f"/runs/{run}/cleanup", follow_redirects=True)
    page = resp.get_data(as_text=True)
    assert "清理失败" in page
    assert "2024-03-03" in page
    assert len(store.days(run)) == 3
    assert len(store.events(run)) == 3
    assert store.get_run(run)["cleaned_at"] is None
    assert len(store.versions()) == 1


def test_cleanup_across_versions_then_versions_survive_restart(tmp_path):
    events = [ev("A", "2024-04-01", 1), ev("B", "2024-04-02", 2), ev("C", "2024-04-03", 3)]
    app = make_app(tmp_path, events)
    client = app.test_client()
    run = create_and_collect(client, "2024-04-01", "2024-04-03")
    store = app.extensions["store"]

    publish(client, "2024-04-01", "2024-04-01", {"2024-04-01": run})
    publish(client, "2024-04-02", "2024-04-03", {"2024-04-02": run, "2024-04-03": run})
    v1, v2 = sorted(row["id"] for row in store.versions())
    assert len(store.versions()) == 2

    resp = client.post(f"/runs/{run}/cleanup", follow_redirects=True)
    page = resp.get_data(as_text=True)
    assert "已清理" in page
    assert store.days(run) == []
    assert store.events(run) == []
    assert store.get_run(run)["cleaned_at"] is not None

    # Published versions keep their content and per-day sources.
    assert [d["run_id"] for d in store.version_days(v1)] == [run]
    assert [d["run_id"] for d in store.version_days(v2)] == [run, run]
    assert [e["event_id"] for e in store.version_events(v1)] == ["A"]
    assert [e["event_id"] for e in store.version_events(v2)] == ["B", "C"]

    # Restart against the same database: everything is still queryable.
    restarted = create_app(database=str(tmp_path / "web.db"), source=app.extensions["source"], testing=True)
    client2 = restarted.test_client()
    restarted_store = restarted.extensions["store"]
    assert [row["id"] for row in restarted_store.versions()] == [v2, v1]
    page2 = client2.get(f"/versions/{v2}").get_data(as_text=True)
    assert "任务 #" in page2 and "B" in page2 and "C" in page2
    csv_text = client2.get(f"/versions/{v2}/download").get_data(as_text=True).strip().splitlines()
    assert csv_text[0] == "event_id,utc_time,magnitude"
    assert len(csv_text) == 3


def test_cleaned_task_cannot_be_source_for_new_version(tmp_path):
    events = [ev("A", "2024-05-01", 1), ev("B", "2024-05-02", 2)]
    app = make_app(tmp_path, events)
    client = app.test_client()
    run_a = create_and_collect(client, "2024-05-01", "2024-05-01")
    run_b = create_and_collect(client, "2024-05-02", "2024-05-02")
    store = app.extensions["store"]

    publish(client, "2024-05-01", "2024-05-01", {"2024-05-01": run_a})
    client.post(f"/runs/{run_a}/cleanup")
    assert store.get_run(run_a)["cleaned_at"] is not None

    resp = publish(
        client,
        "2024-05-01",
        "2024-05-02",
        {"2024-05-01": run_a, "2024-05-02": run_b},
    )
    page = resp.get_data(as_text=True)
    assert "已清理，不能再作为来源" in page
    assert len(store.versions()) == 1

    # The cleaned task also refuses new collection work.
    resp = client.post(f"/runs/{run_a}/collect", follow_redirects=True)
    assert "已清理" in resp.get_data(as_text=True)


def test_cleanup_rolls_back_when_run_already_cleaned(tmp_path):
    app = make_app(tmp_path, [ev("A", "2024-06-01", 1)])
    client = app.test_client()
    run = create_and_collect(client, "2024-06-01", "2024-06-01")
    publish(client, "2024-06-01", "2024-06-01", {"2024-06-01": run})
    assert client.post(f"/runs/{run}/cleanup").status_code == 302

    store = app.extensions["store"]
    cleaned_at = store.get_run(run)["cleaned_at"]
    assert cleaned_at is not None
    resp = client.post(f"/runs/{run}/cleanup", follow_redirects=True)
    assert "清理失败" in resp.get_data(as_text=True)
    # Existing versions are untouched by the failed second cleanup.
    assert len(store.versions()) == 1
    assert store.get_run(run)["cleaned_at"] == cleaned_at


def test_version_new_lists_only_complete_candidate_sources(tmp_path):
    events = [ev(f"X{i:02d}", "2024-07-02", i % 24, i) for i in range(22)]
    app = make_app(tmp_path, events)
    client = app.test_client()
    run = create_and_collect(client, "2024-07-01", "2024-07-02")

    page = client.get(
        "/versions/new?start_date=2024-07-01&end_date=2024-07-02"
    ).get_data(as_text=True)
    # Day 2 is blocked, so it has no candidate; day 1 does.
    assert page.count(f"任务 #{run}") == 1
    assert "没有已查全的来源任务" in page


def test_publish_rejects_non_contiguous_selection(tmp_path):
    app = make_app(tmp_path, [ev("A", "2024-08-01", 1), ev("B", "2024-08-03", 2)])
    client = app.test_client()
    run = create_and_collect(client, "2024-08-01", "2024-08-03")
    store = app.extensions["store"]
    payload = {
        "start_date": "2024-08-01",
        "end_date": "2024-08-03",
        "day": ["2024-08-01", "2024-08-03"],
        "source_2024-08-01": str(run),
        "source_2024-08-03": str(run),
    }
    resp = client.post("/versions", data=payload, follow_redirects=True)
    assert "连续日期" in resp.get_data(as_text=True)
    assert store.versions() == []
    assert len(store.days(run)) == 3


def test_publish_rolls_back_version_rows_on_duplicate(tmp_path):
    """A rejected publish leaves no partial version or copied events behind."""
    store = Store(str(tmp_path / "app.db"))
    store.init()
    run_a = store.create_run(dt.date(2024, 9, 1), dt.date(2024, 9, 1))
    run_b = store.create_run(dt.date(2024, 9, 2), dt.date(2024, 9, 2))
    store.complete_days(
        run_a, {"2024-09-01": [Event("SAME", "2024-09-01 00:00:00", 2.0)]}, source="api"
    )
    store.complete_days(
        run_b, {"2024-09-02": [Event("SAME", "2024-09-02 00:00:00", 3.0)]}, source="api"
    )

    from archive_app.publication import PublishError, verify_publish

    try:
        store.publish_version(
            {"2024-09-01": run_a, "2024-09-02": run_b}, verify=verify_publish
        )
    except PublishError:
        pass
    else:  # pragma: no cover
        raise AssertionError("duplicate ID must abort publication")

    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM version_days").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM version_events").fetchone()[0] == 0
    assert len(store.events(run_a)) == 1
    assert len(store.events(run_b)) == 1


def test_init_migrates_old_database_without_cleaned_at(tmp_path):
    import sqlite3

    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE day_status (
            run_id INTEGER NOT NULL, day TEXT NOT NULL, status TEXT NOT NULL,
            event_count INTEGER NOT NULL DEFAULT 0, checked_at TEXT, last_error TEXT,
            PRIMARY KEY (run_id, day)
        );
        CREATE TABLE events (
            run_id INTEGER NOT NULL, day TEXT NOT NULL, event_id TEXT NOT NULL,
            utc_time TEXT NOT NULL, magnitude REAL NOT NULL, source TEXT NOT NULL,
            PRIMARY KEY (run_id, event_id)
        );
        INSERT INTO runs(start_date, end_date, created_at)
        VALUES ('2024-01-01', '2024-01-01', '2024-01-01T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    store = Store(path)
    store.init()
    run = store.get_run(1)
    assert run["cleaned_at"] is None
    # New version tables exist and work on the migrated database.
    assert store.versions() == []
