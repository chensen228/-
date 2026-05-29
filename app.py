from __future__ import annotations

import html
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
FALLBACK_PAGE_META = {
    "data-center.html": {
        "title": "数据中心",
        "heading": "数据中心云端演示页",
        "description": "Render 免费实例未完整恢复动态分析上下文，当前先展示可访问的云端兜底页。",
        "bullets": [
            "结构化数据主库：SQLite 事务数据",
            "缓存层：Redis 排行、配额、状态摘要",
            "文档层：Mongo 行为日志、画像、质量快照",
            "当前云端部署使用 fallback 存储，适合老师在线查看系统结构与页面入口",
        ],
    },
    "graph-lab.html": {
        "title": "图谱创新",
        "heading": "图谱创新云端演示页",
        "description": "Render 免费实例未完整恢复图谱动态分析上下文，当前先展示可访问的云端兜底页。",
        "bullets": [
            "图模型覆盖学生、课程、教师、图书、实践任务与实验室",
            "本地完整版支持推荐、路径分析、中心性分析与 Cypher 导出",
            "云端免费版保留主页和业务入口，图谱页先以说明性兜底内容替代",
            "答辩时可通过本地运行版演示完整图谱交互能力",
        ],
    },
}
app = Flask(__name__)
app.secret_key = os.getenv("SMART_CAMPUS_SECRET_KEY", "smart-campus-demo-secret")
repo = SmartCampusRepository(BASE_DIR)


def as_int(value: str | None, default: int | None = None) -> int | None:
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def render_plain_fallback(filename: str) -> str:
    meta = FALLBACK_PAGE_META.get(filename, FALLBACK_PAGE_META["data-center.html"])
    bullets = "".join(f"<li>{html.escape(item)}</li>" for item in meta["bullets"])
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(meta['title'])} | 智慧校园大数据管理演示系统</title>
  <style>
    body {{
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f5f7fb;
      color: #18212f;
    }}
    .wrap {{
      max-width: 960px;
      margin: 0 auto;
      padding: 40px 20px 64px;
    }}
    .notice {{
      margin-bottom: 24px;
      padding: 14px 16px;
      border: 1px solid #fcd34d;
      background: #fffbeb;
      color: #92400e;
      border-radius: 12px;
    }}
    .panel {{
      background: #fff;
      border: 1px solid #dbe4f0;
      border-radius: 18px;
      padding: 24px;
      box-shadow: 0 12px 28px rgba(15, 23, 42, 0.08);
    }}
    h1 {{ margin: 0 0 12px; font-size: 32px; }}
    p {{ line-height: 1.7; }}
    ul {{ line-height: 1.8; padding-left: 20px; }}
    .links {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-top: 24px;
    }}
    a {{
      display: inline-block;
      padding: 10px 14px;
      border-radius: 10px;
      text-decoration: none;
      color: #fff;
      background: #2563eb;
    }}
    a.secondary {{
      background: #475569;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="notice">当前页面使用云端演示兜底视图展示。Render 免费实例已成功上线，但该高级分析页的动态上下文未完全恢复。</div>
    <section class="panel">
      <h1>{html.escape(meta['heading'])}</h1>
      <p>{html.escape(meta['description'])}</p>
      <ul>{bullets}</ul>
      <div class="links">
        <a href="/">返回系统首页</a>
        <a href="/library" class="secondary">智慧图书馆</a>
        <a href="/academic" class="secondary">智慧教务</a>
        <a href="/practice" class="secondary">实践教学</a>
        <a href="/governance-lab" class="secondary">数据治理实验室</a>
      </div>
    </section>
  </div>
</body>
</html>"""


def render_dist_fallback(filename: str) -> str:
    dist_path = BASE_DIR / "dist" / filename
    if not dist_path.exists():
        return render_plain_fallback(filename)
    html = dist_path.read_text(encoding="utf-8")
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
