from __future__ import annotations

import csv
import datetime as dt
import io
import os
import threading

from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.exceptions import RequestEntityTooLarge

from .catalog import (
    MAX_SPAN_DAYS,
    STATUS_BLOCKED,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_PENDING,
    Store,
    parse_day,
    source_from_env,
)
from .collector import collect
from .csv_import import CsvValidationError, parse_csv, verify_includes_collected
from .publication import PublishError, verify_publish

STATUS_LABELS = {
    STATUS_PENDING: "待查询",
    STATUS_COMPLETE: "已查全",
    STATUS_BLOCKED: "待补档",
    STATUS_FAILED: "待重试",
}


def _all_days_published(run, published: list[str]) -> bool:
    return set(published) == {
        (dt.date.fromisoformat(run["start_date"]) + dt.timedelta(days=offset)).isoformat()
        for offset in range(
            (dt.date.fromisoformat(run["end_date"]) - dt.date.fromisoformat(run["start_date"])).days
            + 1
        )
    }


def create_app(
    database: str | None = None,
    source=None,
    testing: bool = False,
) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        DATABASE=database or os.environ.get("DATABASE", "/data/archive.db"),
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,
        TESTING=testing,
    )
    store = Store(app.config["DATABASE"])
    store.init()
    app.extensions["store"] = store
    app.extensions["source"] = source or source_from_env()
    work_lock = threading.Lock()

    def current_store() -> Store:
        return app.extensions["store"]

    def current_source():
        return app.extensions["source"]

    @app.template_filter("status_label")
    def status_label(value: str) -> str:
        return STATUS_LABELS.get(value, value)

    @app.get("/")
    def index():
        st: Store = current_store()
        return render_template(
            "index.html",
            runs=st.runs(),
            max_span=MAX_SPAN_DAYS,
            page_size=current_source().page_size(),
        )

    @app.post("/runs")
    def create_run():
        st: Store = current_store()
        try:
            start = parse_day(request.form.get("start_date", ""))
            end = parse_day(request.form.get("end_date", ""))
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        if end < start:
            flash("结束日期不能早于开始日期", "error")
            return redirect(url_for("index"))
        span = (end - start).days + 1
        if span > MAX_SPAN_DAYS:
            flash(f"一次最多选择 {MAX_SPAN_DAYS} 个连续日", "error")
            return redirect(url_for("index"))
        run_id = st.create_run(start, end)
        flash(f"已创建 {start} 至 {end} 的归档任务", "info")
        return redirect(url_for("run_detail", run_id=run_id))

    @app.get("/runs/<int:run_id>")
    def run_detail(run_id: int):
        st: Store = current_store()
        run = st.get_run(run_id)
        if run is None:
            abort(404)
        days = st.days(run_id)
        counts = {status: 0 for status in STATUS_LABELS}
        for row in days:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        cleaned = run["cleaned_at"] is not None
        published = sorted(st.run_published_days(run_id))
        all_published = not cleaned and st.is_ready(run_id) and _all_days_published(run, published)
        return render_template(
            "run.html",
            run=run,
            days=days,
            counts=counts,
            labels=STATUS_LABELS,
            ready=not cleaned and st.is_ready(run_id),
            cleaned=cleaned,
            published=published,
            all_published=all_published,
            events=[] if cleaned else st.events(run_id),
        )

    @app.post("/runs/<int:run_id>/collect")
    def run_collect(run_id: int):
        st: Store = current_store()
        run = st.get_run(run_id)
        if run is None:
            abort(404)
        if run["cleaned_at"] is not None:
            flash("任务已清理，不能再采集", "error")
            return redirect(url_for("run_detail", run_id=run_id))
        if not work_lock.acquire(blocking=False):
            flash("已有采集或补档操作在进行，请稍后重试", "warn")
            return redirect(url_for("run_detail", run_id=run_id))
        try:
            summary = collect(st, current_source(), run_id)
            flash(
                f"本次采集：查全 {summary.completed} 天，待补档 {summary.blocked} 天，"
                f"待重试 {summary.failed} 天",
                "info" if not summary.failed else "warn",
            )
        finally:
            work_lock.release()
        return redirect(url_for("run_detail", run_id=run_id))

    @app.post("/runs/<int:run_id>/days/<day>/upload")
    def upload_day(run_id: int, day: str):
        st: Store = current_store()
        run = st.get_run(run_id)
        if run is None:
            abort(404)
        if run["cleaned_at"] is not None:
            flash("任务已清理，不能再补档", "error")
            return redirect(url_for("run_detail", run_id=run_id))
        rows = [r for r in st.days(run_id) if r["day"] == day]
        if not rows:
            abort(404)
        if rows[0]["status"] != STATUS_BLOCKED:
            flash(f"{day} 不是待补档日期，无需上传", "warn")
            return redirect(url_for("run_detail", run_id=run_id))

        upload = request.files.get("csv")
        if upload is None or not upload.filename:
            flash("请选择 CSV 文件", "error")
            return redirect(url_for("run_detail", run_id=run_id))

        try:
            parsed = parse_csv(upload.read(), day)
            st.backfill_day(
                run_id,
                day,
                parsed.events,
                verify=lambda existing: verify_includes_collected(parsed, existing),
            )
        except CsvValidationError as exc:
            flash("补档文件校验失败，进度保持不变：" + "；".join(exc.errors), "error")
            return redirect(url_for("run_detail", run_id=run_id))
        except ValueError as exc:
            flash(f"补档失败，进度保持不变：{exc}", "error")
            return redirect(url_for("run_detail", run_id=run_id))

        flash(f"{day} 已补档，共 {len(parsed.events)} 条", "info")
        return redirect(url_for("run_detail", run_id=run_id))

    @app.get("/runs/<int:run_id>/download")
    def download_run(run_id: int):
        st: Store = current_store()
        run = st.get_run(run_id)
        if run is None:
            abort(404)
        if run["cleaned_at"] is not None:
            flash("任务已清理，不能再下载完整目录", "error")
            return redirect(url_for("run_detail", run_id=run_id))
        if not st.is_ready(run_id):
            flash("还有未查全或未补档的日期，暂不能下载完整目录", "error")
            return redirect(url_for("run_detail", run_id=run_id))
        events = st.events(run_id)
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["event_id", "utc_time", "magnitude"])
        for row in events:
            writer.writerow([row["event_id"], row["utc_time"], f"{row['magnitude']:g}"])
        filename = f"catalog_{run['start_date']}_{run['end_date']}.csv"
        return Response(
            buffer.getvalue(),
            mimetype="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    # -- published versions -------------------------------------------

    @app.get("/versions")
    def version_list():
        st: Store = current_store()
        return render_template("versions.html", versions=st.versions())

    @app.get("/versions/new")
    def version_new():
        st: Store = current_store()
        try:
            start = parse_day(request.args.get("start_date", ""))
            end = parse_day(request.args.get("end_date", ""))
        except ValueError:
            flash("请选择连续 UTC 日期范围后再选择来源任务", "error")
            return redirect(url_for("index"))
        if end < start:
            flash("结束日期不能早于开始日期", "error")
            return redirect(url_for("index"))
        if (end - start).days + 1 > MAX_SPAN_DAYS:
            flash(f"一次最多选择 {MAX_SPAN_DAYS} 个连续日", "error")
            return redirect(url_for("index"))

        # Candidate sources for each day: the run must cover the day, be
        # already fully archived there, and must not have been cleaned up.
        candidates: dict[str, list[dict]] = {}
        day = start
        while day <= end:
            key = day.isoformat()
            candidates[key] = []
            for run in st.runs():
                if run["cleaned_at"] is not None:
                    continue
                if not (run["start_date"] <= key <= run["end_date"]):
                    continue
                row = next((r for r in st.days(run["id"]) if r["day"] == key), None)
                if row is None or row["status"] != STATUS_COMPLETE:
                    continue
                candidates[key].append(
                    {
                        "run_id": run["id"],
                        "range": f"{run['start_date']} ~ {run['end_date']}",
                        "event_count": row["event_count"],
                    }
                )
            day += dt.timedelta(days=1)
        return render_template(
            "version_new.html",
            start=start.isoformat(),
            end=end.isoformat(),
            days=[d for d in candidates],
            candidates=candidates,
            downloads=[
                {"run_id": r["id"], "range": f"{r['start_date']} ~ {r['end_date']}"}
                for r in st.runs()
                if r["cleaned_at"] is None and st.is_ready(r["id"])
            ],
        )

    @app.post("/versions")
    def version_publish():
        st: Store = current_store()
        try:
            start = parse_day(request.form.get("start_date", ""))
            end = parse_day(request.form.get("end_date", ""))
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))

        sources: dict[str, int] = {}
        days = request.form.getlist("day")
        if not days:
            # Fall back to the contiguous range posted with the form.
            day = start
            while day <= end:
                days.append(day.isoformat())
                day += dt.timedelta(days=1)
        errors: list[str] = []
        if start > end or (end - start).days + 1 > MAX_SPAN_DAYS:
            errors.append("发布日期范围无效或超过上限")
        expected = []
        day = start
        while day <= end:
            expected.append(day.isoformat())
            day += dt.timedelta(days=1)
        if sorted(set(days)) != expected or len(days) != len(set(days)):
            errors.append("所选日期必须与发布的连续日期范围一致")
        for key in days:
            try:
                parse_day(key)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            raw = request.form.get(f"source_{key}", "").strip()
            if not raw:
                errors.append(f"{key} 未指定来源任务")
                continue
            try:
                sources[key] = int(raw)
            except ValueError:
                errors.append(f"{key} 的来源任务无效")
        if errors:
            flash("发布失败，未生成新版本：" + "；".join(errors), "error")
            return redirect(
                url_for("version_new", start_date=start.isoformat(), end_date=end.isoformat())
            )

        try:
            version_id = st.publish_version(sources, verify=verify_publish)
        except (PublishError, ValueError) as exc:
            errors = getattr(exc, "errors", None) or [str(exc)]
            flash("发布失败，未生成新版本：" + "；".join(errors), "error")
            return redirect(
                url_for("version_new", start_date=start.isoformat(), end_date=end.isoformat())
            )

        flash(f"已发布版本 v{version_id}", "info")
        return redirect(url_for("version_detail", version_id=version_id))

    @app.get("/versions/<int:version_id>")
    def version_detail(version_id: int):
        st: Store = current_store()
        version = st.get_version(version_id)
        if version is None:
            abort(404)
        return render_template(
            "version.html",
            version=version,
            days=st.version_days(version_id),
            events=st.version_events(version_id),
        )

    @app.get("/versions/<int:version_id>/download")
    def version_download(version_id: int):
        st: Store = current_store()
        version = st.get_version(version_id)
        if version is None:
            abort(404)
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["event_id", "utc_time", "magnitude"])
        for row in st.version_events(version_id):
            writer.writerow([row["event_id"], row["utc_time"], f"{row['magnitude']:g}"])
        filename = f"catalog_v{version['id']}_{version['start_date']}_{version['end_date']}.csv"
        return Response(
            buffer.getvalue(),
            mimetype="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    @app.post("/runs/<int:run_id>/cleanup")
    def run_cleanup(run_id: int):
        st: Store = current_store()
        if st.get_run(run_id) is None:
            abort(404)
        try:
            st.cleanup_run(run_id)
        except KeyError:
            abort(404)
        except ValueError as exc:
            flash(f"清理失败，任务进度与事件保持不变：{exc}", "error")
            return redirect(url_for("run_detail", run_id=run_id))
        flash(f"任务 #{run_id} 的进度与事件已清理", "info")
        return redirect(url_for("run_detail", run_id=run_id))

    @app.errorhandler(404)
    def not_found(_exc):
        return render_template("404.html"), 404

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_exc):
        flash("补档文件过大（上限 4 MB）", "error")
        return redirect(request.referrer or url_for("index"))

    return app
