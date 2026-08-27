from __future__ import annotations

import os
import threading
import webbrowser

from flask import Flask, jsonify, render_template, request, send_file

from project_store import ProjectConflict, ProjectStore


store = ProjectStore()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024


def ok(data=None, revision: int | None = None):
    body = {"ok": True, "data": data}
    if revision is not None:
        body["revision"] = revision
    return jsonify(body)


def fail(exc: Exception, status: int = 400, revision: int | None = None):
    body = {"ok": False, "error": str(exc)}
    if revision is not None:
        body["revision"] = revision
    return jsonify(body), status


def project_id() -> str:
    value = request.headers.get("X-Project-ID") or request.args.get("project_id")
    if not value:
        raise ValueError("Сначала откройте проект")
    return value


def expected_revision() -> int | None:
    raw = request.headers.get("X-Project-Revision")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError("Некорректная ревизия проекта") from exc


def project_mutation(callback):
    pid = project_id()
    try:
        result, revision = store.mutate(pid, callback, expected_revision())
        return ok(result, revision)
    except ProjectConflict as exc:
        return fail(exc, 409, exc.current_revision)


@app.get("/")
def index():
    return render_template("index.html")


# ---------- projects ----------
@app.get("/api/projects")
def list_projects():
    return ok(store.list_projects())


@app.post("/api/projects")
def create_project():
    try:
        payload = request.get_json(force=True)
        return ok(store.create_project(payload.get("name", "")))
    except Exception as exc:
        return fail(exc)


@app.put("/api/projects/<pid>")
def rename_project(pid: str):
    try:
        payload = request.get_json(force=True)
        meta = store.rename_project(pid, payload.get("name", ""))
        return ok(meta, int(meta.get("revision", 0)))
    except Exception as exc:
        return fail(exc)


@app.delete("/api/projects/<pid>")
def delete_project(pid: str):
    try:
        store.delete_project(pid)
        return ok()
    except Exception as exc:
        return fail(exc)


@app.get("/api/projects/<pid>/revision")
def get_project_revision(pid: str):
    try:
        return ok({"revision": store.get_revision(pid)})
    except Exception as exc:
        return fail(exc, 404)


# ---------- project workspace ----------
@app.get("/api/state")
def get_state():
    try:
        state, revision = store.state(project_id())
        return ok(state, revision)
    except Exception as exc:
        return fail(exc, 404)


@app.post("/api/import")
def import_excel():
    try:
        if "file" not in request.files:
            raise ValueError("Файл не выбран")
        f = request.files["file"]
        if not f.filename:
            raise ValueError("Файл не выбран")

        state, revision = store.import_excel(
            project_id(),
            f.read(),
            f.filename,
            expected_revision(),
        )
        # Add current project metadata to the just-imported state.
        full_state, _ = store.state(project_id())
        return ok(full_state, revision)
    except ProjectConflict as exc:
        return fail(exc, 409, exc.current_revision)
    except Exception as exc:
        return fail(exc)


@app.get("/api/export")
def export_excel():
    try:
        content, filename = store.export_excel(project_id())
        mimetype = (
            "application/vnd.ms-excel.sheet.macroEnabled.12"
            if filename.lower().endswith(".xlsm")
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        return send_file(
            content,
            mimetype=mimetype,
            as_attachment=True,
            download_name=filename,
        )
    except Exception as exc:
        return fail(exc)


@app.post("/api/sites")
def create_site():
    try:
        return project_mutation(
            lambda ws: ws.create_site(request.get_json(force=True))
        )
    except Exception as exc:
        return fail(exc)


@app.put("/api/sites/<site_id>")
def update_site(site_id: str):
    try:
        return project_mutation(
            lambda ws: ws.update_site(site_id, request.get_json(force=True))
        )
    except Exception as exc:
        return fail(exc)


@app.delete("/api/sites/<site_id>")
def delete_site(site_id: str):
    try:
        return project_mutation(lambda ws: ws.delete_site(site_id))
    except Exception as exc:
        return fail(exc)


@app.post("/api/subnets")
def create_subnet():
    try:
        return project_mutation(
            lambda ws: ws.create_subnet(request.get_json(force=True))
        )
    except Exception as exc:
        return fail(exc)


@app.put("/api/subnets/<subnet_id>")
def update_subnet(subnet_id: str):
    try:
        return project_mutation(
            lambda ws: ws.update_subnet(subnet_id, request.get_json(force=True))
        )
    except Exception as exc:
        return fail(exc)


@app.delete("/api/subnets/<subnet_id>")
def delete_subnet(subnet_id: str):
    try:
        return project_mutation(lambda ws: ws.delete_subnet(subnet_id))
    except Exception as exc:
        return fail(exc)


@app.post("/api/subnets/<subnet_id>/hosts")
def create_host(subnet_id: str):
    try:
        return project_mutation(
            lambda ws: {
                "id": ws.create_host(subnet_id, request.get_json(force=True))
            }
        )
    except Exception as exc:
        return fail(exc)


@app.put("/api/hosts/<host_id>")
def update_host(host_id: str):
    try:
        return project_mutation(
            lambda ws: ws.update_host(host_id, request.get_json(force=True))
        )
    except Exception as exc:
        return fail(exc)


@app.delete("/api/hosts/<host_id>")
def delete_host(host_id: str):
    try:
        return project_mutation(lambda ws: ws.delete_host(host_id))
    except Exception as exc:
        return fail(exc)


@app.get("/api/health")
def health():
    return ok({"status": "up"})


def open_browser():
    webbrowser.open("http://127.0.0.1:5080")


if __name__ == "__main__":
    if os.environ.get("IP_PLAN_NO_BROWSER") != "1":
        threading.Timer(1.0, open_browser).start()

    app.run(
        host=os.environ.get("IP_PLAN_HOST", "127.0.0.1"),
        port=int(os.environ.get("IP_PLAN_PORT", "5080")),
        debug=False,
        use_reloader=False,
        threaded=True,
    )
