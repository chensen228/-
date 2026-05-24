from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, url_for

from demo_backend import SmartCampusRepository


BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__)
app.secret_key = os.getenv("SMART_CAMPUS_SECRET_KEY", "smart-campus-demo-secret")
repo = SmartCampusRepository(BASE_DIR)


def as_int(value: str | None, default: int | None = None) -> int | None:
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


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
    return render_template("data_center.html", active_nav="data-center", **repo.data_center_overview())


@app.get("/governance-lab")
def governance_lab():
    return render_template("governance_lab.html", active_nav="governance-lab", **repo.governance_overview())


@app.get("/graph-lab")
def graph_lab():
    student_id = as_int(request.args.get("student_id"))
    return render_template("graph_lab.html", active_nav="graph-lab", **repo.graph_overview(student_id))


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
    host = os.getenv("SMART_CAMPUS_HOST", "0.0.0.0" if os.getenv("RENDER") == "true" or os.getenv("PORT") else "127.0.0.1")
    port = int(os.getenv("PORT", os.getenv("SMART_CAMPUS_PORT", "5050")))
    app.run(host=host, port=port, debug=False)
