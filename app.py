from __future__ import annotations

import os
import threading
import webbrowser

from flask import Flask, jsonify, render_template, request, send_file

from project_store import (
    ProjectAccessDenied,
    ProjectConflict,
    ProjectNotFound,
    ProjectStore,
)


def create_app(project_store: ProjectStore | None = None) -> Flask:
    store = project_store or ProjectStore()
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
    app.extensions["project_store"] = store

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

    def authorize(pid: str) -> None:
        store.verify_access(pid, request.headers.get("X-Project-Token", ""))

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
        authorize(pid)
        try:
            result, revision = store.mutate(pid, callback, expected_revision())
            return ok(result, revision)
        except ProjectConflict as exc:
            return fail(exc, 409, exc.current_revision)

    @app.errorhandler(ProjectAccessDenied)
    def access_denied(exc):
        return fail(exc, 403)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/projects")
    def list_projects():
        return ok(store.list_projects())

    @app.post("/api/projects")
    def create_project():
        try:
            payload = request.get_json(force=True)
            project, access_token = store.create_project(
                payload.get("name", ""), payload.get("pin", "")
            )
            return ok({"project": project, "access_token": access_token})
        except Exception as exc:
            return fail(exc)

    @app.post("/api/projects/<pid>/unlock")
    def unlock_project(pid: str):
        try:
            payload = request.get_json(force=True)
            return ok(store.unlock_project(pid, payload.get("pin", "")))
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except ProjectNotFound as exc:
            return fail(exc, 404)
        except Exception as exc:
            return fail(exc)

    @app.put("/api/projects/<pid>")
    def rename_project(pid: str):
        try:
            authorize(pid)
            payload = request.get_json(force=True)
            meta = store.rename_project(pid, payload.get("name", ""))
            return ok(meta, int(meta.get("revision", 0)))
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/projects/<pid>")
    def delete_project(pid: str):
        try:
            authorize(pid)
            store.delete_project(pid)
            return ok()
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc)

    @app.get("/api/projects/<pid>/revision")
    def get_project_revision(pid: str):
        try:
            authorize(pid)
            return ok({"revision": store.get_revision(pid)})
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc, 404)

    @app.get("/api/state")
    def get_state():
        try:
            pid = project_id()
            authorize(pid)
            state, revision = store.state(pid)
            return ok(state, revision)
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc, 404)

    @app.post("/api/import")
    def import_excel():
        try:
            pid = project_id()
            authorize(pid)
            if "file" not in request.files:
                raise ValueError("Файл не выбран")
            uploaded = request.files["file"]
            if not uploaded.filename:
                raise ValueError("Файл не выбран")

            _, revision = store.import_excel(
                pid,
                uploaded.read(),
                uploaded.filename,
                expected_revision(),
            )
            full_state, _ = store.state(pid)
            return ok(full_state, revision)
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except ProjectConflict as exc:
            return fail(exc, 409, exc.current_revision)
        except Exception as exc:
            return fail(exc)

    @app.get("/api/export")
    def export_excel():
        try:
            pid = project_id()
            authorize(pid)
            content, filename = store.export_excel(pid)
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
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc)

    @app.post("/api/sites")
    def create_site():
        try:
            return project_mutation(
                lambda workspace: workspace.create_site(request.get_json(force=True))
            )
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc)

    @app.put("/api/sites/<site_id>")
    def update_site(site_id: str):
        try:
            return project_mutation(
                lambda workspace: workspace.update_site(
                    site_id, request.get_json(force=True)
                )
            )
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/sites/<site_id>")
    def delete_site(site_id: str):
        try:
            return project_mutation(lambda workspace: workspace.delete_site(site_id))
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc)

    @app.post("/api/subnets")
    def create_subnet():
        try:
            return project_mutation(
                lambda workspace: workspace.create_subnet(request.get_json(force=True))
            )
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc)

    @app.put("/api/subnets/<subnet_id>")
    def update_subnet(subnet_id: str):
        try:
            return project_mutation(
                lambda workspace: workspace.update_subnet(
                    subnet_id, request.get_json(force=True)
                )
            )
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/subnets/<subnet_id>")
    def delete_subnet(subnet_id: str):
        try:
            return project_mutation(lambda workspace: workspace.delete_subnet(subnet_id))
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc)

    @app.post("/api/subnets/<subnet_id>/hosts")
    def create_host(subnet_id: str):
        try:
            return project_mutation(
                lambda workspace: {
                    "id": workspace.create_host(
                        subnet_id, request.get_json(force=True)
                    )
                }
            )
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc)

    @app.put("/api/hosts/<host_id>")
    def update_host(host_id: str):
        try:
            return project_mutation(
                lambda workspace: workspace.update_host(
                    host_id, request.get_json(force=True)
                )
            )
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/hosts/<host_id>")
    def delete_host(host_id: str):
        try:
            return project_mutation(lambda workspace: workspace.delete_host(host_id))
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc)

    @app.get("/api/health")
    def health():
        return ok({"status": "up"})

    return app


store = ProjectStore()
app = create_app(store)


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
