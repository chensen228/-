from __future__ import annotations

import os
import traceback
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, url_for

from demo_backend import SmartCampusRepository


BASE_DIR = Path(__file__).resolve().parent
RUNNING_ON_RENDER = any(os.getenv(name) for name in ("RENDER", "RENDER_SERVICE_ID", "RENDER_EXTERNAL_URL"))
DIST_ROUTE_REPLACEMENTS = {
    "./index.html": "/",
    "./library.html": "/library",
    "./academic.html": "/academic",
    "./practice.html": "/practice",
    "./data-center.html": "/data-center",
    "./governance-lab.html": "/governance-lab",
    "./graph-lab.html": "/graph-lab",
    "./static/": "/static/",
}
DIST_RUNTIME_REPLACEMENTS = {
    "真实 Redis（127.0.0.1:6379/db0）": "云端回退缓存模式",
    "127.0.0.1:6379 / db0": "Render 免费实例 · 内置 JSON fallback",
    "真实 MongoDB（127.0.0.1:27017/smart_campus）": "云端回退文档模式",
    "文档留痕、Geo、TTL、GridFS 与质量快照": "Render 免费实例 · 内置 JSON 文档 fallback",
    "Neo4j 已连接（bolt://127.0.0.1:7687）": "本地图分析模式",
    "Cypher 导出、推荐关系和中心性分析": "Render 免费实例未接入 Neo4j，保留图分析展示",
}
app = Flask(__name__)
app.secret_key = os.getenv("SMART_CAMPUS_SECRET_KEY", "smart-campus-demo-secret")
repo = SmartCampusRepository(BASE_DIR)


def as_int(value: str | None, default: int | None = None) -> int | None:
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def render_dist_fallback(filename: str) -> str:
    html = (BASE_DIR / "dist" / filename).read_text(encoding="utf-8")
    for source, target in DIST_ROUTE_REPLACEMENTS.items():
        html = html.replace(source, target)
    for source, target in DIST_RUNTIME_REPLACEMENTS.items():
        html = html.replace(source, target)
    notice = (
        '<div style="padding:12px 16px;background:#fef3c7;color:#92400e;'
        'font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
        'border-bottom:1px solid #fcd34d;">'
        "当前页面使用云端演示兜底视图展示，原因是 Render 免费实例的动态分析上下文未完全恢复。"
        "</div>"
    )
    return html.replace("<body>", f"<body>{notice}", 1)


def render_with_recovery(template_name: str, active_nav: str, context_builder, dist_fallback: str | None = None):
    try:
        return render_template(template_name, active_nav=active_nav, **context_builder())
    except Exception:
        if not RUNNING_ON_RENDER:
            raise
        traceback.print_exc()
        try:
            repo.reset_demo()
            return render_template(template_name, active_nav=active_nav, **context_builder())
        except Exception:
            traceback.print_exc()
            if dist_fallback:
                return render_dist_fallback(dist_fallback)
            raise


@app.context_processor
def inject_runtime_context():
    return {
        "redis_runtime": repo.redis_runtime(),
        "mongo_runtime": repo.mongo_runtime(),
        "graph_runtime": repo.graph_runtime(),
    }


@app.get("/")
def index():
    return render_template("index.html", dashboard=repo.dashboard_context(), active_nav="home")


@app.get("/library")
def library():
    keyword = request.args.get("keyword", "")
    return render_template("library.html", active_nav="library", **repo.library_overview(keyword))


@app.post("/library/borrow")
def library_borrow():
    success, message = repo.borrow_book(as_int(request.form.get("student_id"), 0) or 0, as_int(request.form.get("book_id"), 0) or 0)
    flash(message, "success" if success else "error")
    return redirect(url_for("library"))


@app.post("/library/return")
def library_return():
    success, message = repo.return_book(as_int(request.form.get("record_id"), 0) or 0)
    flash(message, "success" if success else "error")
    return redirect(url_for("library"))


@app.post("/library/reserve-seat")
def library_reserve_seat():
    success, message = repo.reserve_seat(
        as_int(request.form.get("student_id"), 0) or 0,
        as_int(request.form.get("seat_id"), 0) or 0,
        request.form.get("reserve_date", ""),
        request.form.get("time_slot", ""),
    )
    flash(message, "success" if success else "error")
    return redirect(url_for("library"))


@app.get("/academic")
def academic():
    student_id = as_int(request.args.get("student_id"))
    return render_template("academic.html", active_nav="academic", **repo.academic_overview(student_id))


@app.post("/academic/select")
def academic_select():
    student_id = as_int(request.form.get("student_id"), 0) or 0
    offering_id = as_int(request.form.get("offering_id"), 0) or 0
    success, message = repo.select_course(student_id, offering_id)
    flash(message, "success" if success else "error")
    return redirect(url_for("academic", student_id=student_id))


@app.post("/academic/drop")
def academic_drop():
    success, message, student_id = repo.drop_course(as_int(request.form.get("selection_id"), 0) or 0)
    flash(message, "success" if success else "error")
    return redirect(url_for("academic", student_id=student_id or ""))


@app.get("/practice")
def practice():
    student_id = as_int(request.args.get("student_id"))
    return render_template("practice.html", active_nav="practice", **repo.practice_overview(student_id))


@app.post("/practice/book-lab")
def practice_book_lab():
    student_id = as_int(request.form.get("student_id"), 0) or 0
    success, message = repo.book_lab(
        student_id,
        as_int(request.form.get("room_id"), 0) or 0,
        as_int(request.form.get("project_id"), 0) or 0,
        request.form.get("booking_date", ""),
        request.form.get("time_slot", ""),
    )
    flash(message, "success" if success else "error")
    return redirect(url_for("practice", student_id=student_id))


@app.post("/practice/sign")
def practice_sign():
    student_id = as_int(request.form.get("student_id"))
    success, message = repo.sign_attendance(as_int(request.form.get("task_id"), 0) or 0, request.form.get("location", ""))
    flash(message, "success" if success else "error")
    return redirect(url_for("practice", student_id=student_id or ""))


@app.post("/practice/report")
def practice_report():
    student_id = as_int(request.form.get("student_id"))
    success, message = repo.submit_weekly_report(
        as_int(request.form.get("task_id"), 0) or 0,
        as_int(request.form.get("week_no"), 1) or 1,
        request.form.get("content", ""),
    )
    flash(message, "success" if success else "error")
    return redirect(url_for("practice", student_id=student_id or ""))


@app.get("/data-center")
def data_center():
    return render_with_recovery("data_center.html", "data-center", repo.data_center_overview, dist_fallback="data-center.html")


@app.get("/governance-lab")
def governance_lab():
    return render_template("governance_lab.html", active_nav="governance-lab", **repo.governance_overview())


@app.get("/graph-lab")
def graph_lab():
    student_id = as_int(request.args.get("student_id"))
    return render_with_recovery("graph_lab.html", "graph-lab", lambda: repo.graph_overview(student_id), dist_fallback="graph-lab.html")


@app.post("/reset-demo")
def reset_demo():
    repo.reset_demo()
    flash("演示数据已重置为初始状态。", "success")
    return redirect(request.referrer or url_for("index"))


@app.get("/healthz")
def healthz():
    return {
        "status": "ok",
        "app": "smart-campus-demo",
        "redis_mode": repo.redis_runtime()["mode"],
        "mongo_mode": repo.mongo_runtime()["mode"],
        "graph_mode": repo.graph_runtime()["mode"],
    }


if __name__ == "__main__":
    host = os.getenv("SMART_CAMPUS_HOST", "0.0.0.0" if RUNNING_ON_RENDER or os.getenv("PORT") else "127.0.0.1")
    port = int(os.getenv("PORT", os.getenv("SMART_CAMPUS_PORT", "5050")))
    app.run(host=host, port=port, debug=False)
