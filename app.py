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
    UserAccessDenied,
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
        actor = current_user(required=False)
        if actor:
            store.claim_legacy_project_owner(pid, actor)

    def current_user(required: bool = True):
        token = request.headers.get("X-User-Token", "")
        if not token and not required:
            return None
        return store.verify_user(token)

    def expected_revision() -> int | None:
        raw = request.headers.get("X-Project-Revision")
        if raw in (None, ""):
            return None
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError("Некорректная ревизия проекта") from exc

    def project_mutation(callback, audit_builder=None):
        try:
            pid = project_id()
            user = current_user()
            assert user is not None
            authorize(pid)
            result, revision = store.mutate(
                pid,
                callback,
                expected_revision(),
                actor=user,
                audit_builder=audit_builder,
            )
            return ok(result, revision)
        except UserAccessDenied as exc:
            return fail(exc, 401)
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except ProjectConflict as exc:
            return fail(exc, 409, exc.current_revision)

    @app.errorhandler(ProjectAccessDenied)
    def access_denied(exc):
        return fail(exc, 403)

    @app.errorhandler(UserAccessDenied)
    def user_access_denied(exc):
        return fail(exc, 401)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.post("/api/users")
    def create_user():
        try:
            payload = request.get_json(force=True)
            user, access_token = store.create_user(payload.get("name", ""))
            return ok({"user": user, "access_token": access_token})
        except Exception as exc:
            return fail(exc)

    @app.get("/api/users/me")
    def get_current_user():
        try:
            return ok(current_user())
        except UserAccessDenied as exc:
            return fail(exc, 401)

    @app.put("/api/users/me")
    def update_current_user():
        try:
            payload = request.get_json(force=True)
            user = store.update_user(
                request.headers.get("X-User-Token", ""), payload.get("name", "")
            )
            return ok(user)
        except UserAccessDenied as exc:
            return fail(exc, 401)
        except Exception as exc:
            return fail(exc)

    @app.get("/api/projects")
    def list_projects():
        try:
            user = current_user(required=False)
            projects = store.list_projects()
            for project in projects:
                project["can_delete"] = bool(
                    user and store.is_creator(project["id"], user["id"])
                )
            return ok(projects)
        except UserAccessDenied as exc:
            return fail(exc, 401)

    @app.post("/api/projects")
    def create_project():
        try:
            user = current_user()
            assert user is not None
            payload = request.get_json(force=True)
            project, access_token = store.create_project(
                payload.get("name", ""),
                payload.get("pin", ""),
                user["id"],
                user["name"],
            )
            project["can_delete"] = True
            return ok({"project": project, "access_token": access_token})
        except UserAccessDenied as exc:
            return fail(exc, 401)
        except Exception as exc:
            return fail(exc)

    @app.post("/api/projects/<pid>/unlock")
    def unlock_project(pid: str):
        try:
            user = current_user()
            assert user is not None
            payload = request.get_json(force=True)
            result = store.unlock_project(
                pid, payload.get("pin", ""), user["id"], user["name"]
            )
            result["project"]["can_delete"] = store.is_creator(pid, user["id"])
            return ok(result)
        except UserAccessDenied as exc:
            return fail(exc, 401)
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except ProjectNotFound as exc:
            return fail(exc, 404)
        except Exception as exc:
            return fail(exc)

    @app.put("/api/projects/<pid>")
    def rename_project(pid: str):
        try:
            user = current_user()
            assert user is not None
            authorize(pid)
            payload = request.get_json(force=True)
            meta = store.rename_project(pid, payload.get("name", ""), actor=user)
            return ok(meta, int(meta.get("revision", 0)))
        except UserAccessDenied as exc:
            return fail(exc, 401)
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/projects/<pid>")
    def delete_project(pid: str):
        try:
            user = current_user()
            assert user is not None
            authorize(pid)
            store.delete_project(pid, user)
            return ok()
        except UserAccessDenied as exc:
            return fail(exc, 401)
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc)

    @app.get("/api/projects/<pid>/revision")
    def get_project_revision(pid: str):
        try:
            authorize(pid)
            return ok({"revision": store.get_revision(pid)})
        except UserAccessDenied as exc:
            return fail(exc, 401)
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
        except UserAccessDenied as exc:
            return fail(exc, 401)
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc, 404)

    @app.get("/api/audit")
    def get_audit_log():
        try:
            pid = project_id()
            current_user()
            authorize(pid)
            return ok(store.audit_log(pid))
        except UserAccessDenied as exc:
            return fail(exc, 401)
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc, 404)

    @app.get("/api/system-audit")
    def get_system_audit_log():
        try:
            current_user()
            return ok(store.system_audit_log())
        except UserAccessDenied as exc:
            return fail(exc, 401)
        except Exception as exc:
            return fail(exc, 500)

    @app.post("/api/import")
    def import_excel():
        try:
            pid = project_id()
            user = current_user()
            assert user is not None
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
                actor=user,
            )
            full_state, _ = store.state(pid)
            return ok(full_state, revision)
        except UserAccessDenied as exc:
            return fail(exc, 401)
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
        except UserAccessDenied as exc:
            return fail(exc, 401)
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc)

    @app.post("/api/sites")
    def create_site():
        try:
            return project_mutation(
                lambda workspace: workspace.create_site(request.get_json(force=True)),
                lambda workspace, result: {
                    "action": "site_created",
                    "description": (
                        f"создал(а) площадку «{workspace.find_site(result['id'])['name']}»"
                    ),
                    "target_type": "site",
                    "target_id": result["id"],
                    "anchor": f"site-{result['id']}",
                },
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
                ),
                lambda workspace, result: {
                    "action": "site_updated",
                    "description": f"изменил(а) площадку «{result['name']}»",
                    "target_type": "site",
                    "target_id": site_id,
                    "anchor": f"site-{site_id}",
                },
            )
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/sites/<site_id>")
    def delete_site(site_id: str):
        deleted: dict[str, str] = {}

        def action(workspace):
            site = workspace.find_site(site_id)
            deleted["name"] = site["name"]
            return workspace.delete_site(site_id)

        try:
            return project_mutation(
                action,
                lambda workspace, result: {
                    "action": "site_deleted",
                    "description": f"удалил(а) площадку «{deleted['name']}»",
                    "target_type": "site",
                    "target_id": site_id,
                    "anchor": f"site-{site_id}",
                },
            )
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc)

    @app.post("/api/subnets")
    def create_subnet():
        try:
            return project_mutation(
                lambda workspace: workspace.create_subnet(request.get_json(force=True)),
                lambda workspace, result: {
                    "action": "subnet_created",
                    "description": (
                        f"создал(а) подсеть {workspace.find_subnet(result['id'])[1]['cidr']}"
                    ),
                    "target_type": "subnet",
                    "target_id": result["id"],
                    "anchor": f"row-{result['id']}",
                },
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
                ),
                lambda workspace, result: {
                    "action": "subnet_updated",
                    "description": f"изменил(а) подсеть {result['cidr']}",
                    "target_type": "subnet",
                    "target_id": subnet_id,
                    "anchor": f"row-{subnet_id}",
                },
            )
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/subnets/<subnet_id>")
    def delete_subnet(subnet_id: str):
        deleted: dict[str, str] = {}

        def action(workspace):
            site, subnet = workspace.find_subnet(subnet_id)
            deleted["cidr"] = subnet["cidr"]
            parent_id = workspace.subnet_parent_id(site, subnet)
            deleted["anchor"] = (
                f"site-{site['id']}" if parent_id == site["id"] else f"row-{parent_id}"
            )
            return workspace.delete_subnet(subnet_id)

        try:
            return project_mutation(
                action,
                lambda workspace, result: {
                    "action": "subnet_deleted",
                    "description": f"удалил(а) подсеть {deleted['cidr']}",
                    "target_type": "subnet",
                    "target_id": subnet_id,
                    "anchor": deleted["anchor"],
                },
            )
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
                },
                lambda workspace, result: {
                    "action": "host_created",
                    "description": (
                        f"создал(а) хост {workspace.find_host(result['id'])[2]['values'][0]}"
                    ),
                    "target_type": "host",
                    "target_id": result["id"],
                    "anchor": f"row-{result['id']}",
                },
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
                ),
                lambda workspace, result: {
                    "action": "host_updated",
                    "description": (
                        f"изменил(а) хост {workspace.find_host(host_id)[2]['values'][0]}"
                    ),
                    "target_type": "host",
                    "target_id": host_id,
                    "anchor": f"row-{host_id}",
                },
            )
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/hosts/<host_id>")
    def delete_host(host_id: str):
        deleted: dict[str, str] = {}

        def action(workspace):
            site, subnet, host = workspace.find_host(host_id)
            deleted["ip"] = str(host["values"][0])
            deleted["anchor"] = f"row-{subnet['id']}"
            return workspace.delete_host(host_id)

        try:
            return project_mutation(
                action,
                lambda workspace, result: {
                    "action": "host_deleted",
                    "description": f"удалил(а) хост {deleted['ip']}",
                    "target_type": "host",
                    "target_id": host_id,
                    "anchor": deleted["anchor"],
                },
            )
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
