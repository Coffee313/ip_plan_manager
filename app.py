from __future__ import annotations

import os
import threading
import webbrowser
import ipaddress
from copy import deepcopy
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

from backup import cleanup_old_backups, create_backup
from project_store import (
    ProjectAccessDenied,
    ProjectConflict,
    ProjectNotFound,
    ProjectStore,
    UserAccessDenied,
)
from restore_backup import list_project_backups, restore_project_backup


def create_app(project_store: ProjectStore | None = None) -> Flask:
    store = project_store or ProjectStore()
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
    app.extensions["project_store"] = store

    def changed_values(
        before: dict[str, object], after: dict[str, object]
    ) -> tuple[dict[str, object], dict[str, object]]:
        keys = list(dict.fromkeys([*before, *after]))
        changed = [key for key in keys if before.get(key) != after.get(key)]
        return (
            {key: before.get(key, "") for key in changed},
            {key: after.get(key, "") for key in changed},
        )

    def site_values(site: dict) -> dict[str, object]:
        return {"Название": site["name"], "CIDR": site["cidr"]}

    def subnet_values(subnet: dict) -> dict[str, object]:
        values = list(subnet.get("values") or []) + [None] * 17
        return {
            "CIDR": subnet["cidr"],
            "Gateway": values[1] or "",
            "VRF": values[2] or "",
            "VLAN": values[3] if values[3] is not None else "",
            "VLAN Name": values[4] or "",
            "Комментарий": values[5] or "",
            "Зона": values[6] or "",
            "Площадка": values[7] or "",
            "Описание": values[8] or "",
        }

    def host_values(host: dict) -> dict[str, object]:
        values = list(host.get("values") or []) + [None] * 17
        return {
            "IP": values[0] or "",
            "Имя": values[10] or "",
            "Роль": values[11] or "",
            "Подсистема": values[9] or "",
            "CPU": values[12] or "",
            "RAM": values[13] or "",
            "Диск": values[14] or "",
            "Тип": values[15] or "",
            "Статус": values[16] or "",
            "Комментарий": values[5] or "",
        }

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

    @app.post("/api/undo")
    def undo_own_change():
        try:
            pid = project_id()
            user = current_user()
            assert user is not None
            authorize(pid)
            result, revision = store.undo_last_change(pid, user, expected_revision())
            return ok(result, revision)
        except UserAccessDenied as exc:
            return fail(exc, 401)
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except ProjectConflict as exc:
            return fail(exc, 409, exc.current_revision)
        except Exception as exc:
            return fail(exc)

    def require_project_owner(pid: str) -> dict:
        user = current_user()
        assert user is not None
        authorize(pid)
        if not store.is_creator(pid, user["id"]):
            raise ProjectAccessDenied("Только владелец проекта может управлять бэкапами")
        return user

    @app.get("/api/projects/<pid>/backups")
    def get_project_backups(pid: str):
        try:
            require_project_owner(pid)
            return ok(list_project_backups(store, pid))
        except UserAccessDenied as exc:
            return fail(exc, 401)
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc)

    @app.post("/api/projects/<pid>/backups")
    def create_project_backup(pid: str):
        try:
            require_project_owner(pid)
            path = create_backup(store)
            cleanup_old_backups(store)
            return ok({"filename": path.name})
        except UserAccessDenied as exc:
            return fail(exc, 401)
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc)

    @app.post("/api/projects/<pid>/backups/<filename>/restore")
    def restore_project_from_backup(pid: str, filename: str):
        try:
            user = require_project_owner(pid)
            backup_path = store.backups_root / Path(filename).name
            revision = restore_project_backup(backup_path, pid, user, store)
            state, _ = store.state(pid)
            return ok(state, revision)
        except UserAccessDenied as exc:
            return fail(exc, 401)
        except ProjectAccessDenied as exc:
            return fail(exc, 403)
        except Exception as exc:
            return fail(exc)

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
                    "undo": {
                        "kind": "site_created", "target_id": result["id"], "before": {},
                        "after": site_values(workspace.find_site(result["id"])),
                    },
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
        changed: dict[str, dict[str, object]] = {}

        def action(workspace):
            before = site_values(workspace.find_site(site_id))
            result = workspace.update_site(site_id, request.get_json(force=True))
            after = site_values(workspace.find_site(site_id))
            changed["before"], changed["after"] = changed_values(
                before, after
            )
            changed["undo"] = {"kind": "site", "target_id": site_id, "before": before, "after": after}
            return result

        try:
            return project_mutation(
                action,
                lambda workspace, result: {
                    "action": "site_updated",
                    "description": f"изменил(а) площадку «{result['name']}»",
                    "before": changed["before"],
                    "after": changed["after"],
                    "undo": changed["undo"],
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
                    "undo": {
                        "kind": "subnet_created", "target_id": result["id"], "before": {},
                        "after": subnet_values(workspace.find_subnet(result["id"])[1]),
                    },
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
        changed: dict[str, dict[str, object]] = {}

        def action(workspace):
            before = subnet_values(workspace.find_subnet(subnet_id)[1])
            result = workspace.update_subnet(subnet_id, request.get_json(force=True))
            after = subnet_values(workspace.find_subnet(subnet_id)[1])
            changed["before"], changed["after"] = changed_values(
                before, after
            )
            changed["undo"] = {"kind": "subnet", "target_id": subnet_id, "before": before, "after": after}
            return result

        try:
            return project_mutation(
                action,
                lambda workspace, result: {
                    "action": "subnet_updated",
                    "description": f"изменил(а) подсеть {result['cidr']}",
                    "before": changed["before"],
                    "after": changed["after"],
                    "undo": changed["undo"],
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
        deleted: dict[str, object] = {}

        def action(workspace):
            site, subnet = workspace.find_subnet(subnet_id)
            deleted["cidr"] = subnet["cidr"]
            deleted["before"] = subnet_values(subnet)
            deleted["site_id"] = site["id"]
            target = ipaddress.ip_network(subnet["cidr"], strict=False)
            deleted["snapshots"] = deepcopy([
                item for item in site["subnets"]
                if ipaddress.ip_network(item["cidr"], strict=False).subnet_of(target)
            ])
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
                    "before": deleted["before"],
                    "after": {"Состояние": "Удалено"},
                    "undo": {
                        "kind": "subnet_deleted", "target_id": subnet_id,
                        "site_id": deleted["site_id"], "snapshots": deleted["snapshots"],
                    },
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
                    "undo": {
                        "kind": "host_created", "target_id": result["id"], "before": {},
                        "after": host_values(workspace.find_host(result["id"])[2]),
                    },
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
        changed: dict[str, dict[str, object]] = {}

        def action(workspace):
            before = host_values(workspace.find_host(host_id)[2])
            result = workspace.update_host(host_id, request.get_json(force=True))
            after = host_values(workspace.find_host(host_id)[2])
            changed["before"], changed["after"] = changed_values(
                before, after
            )
            changed["undo"] = {"kind": "host", "target_id": host_id, "before": before, "after": after}
            return result

        try:
            return project_mutation(
                action,
                lambda workspace, result: {
                    "action": "host_updated",
                    "description": (
                        f"изменил(а) хост {workspace.find_host(host_id)[2]['values'][0]}"
                    ),
                    "before": changed["before"],
                    "after": changed["after"],
                    "undo": changed["undo"],
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
        deleted: dict[str, object] = {}

        def action(workspace):
            site, subnet, host = workspace.find_host(host_id)
            deleted["ip"] = str(host["values"][0])
            deleted["anchor"] = f"row-{subnet['id']}"
            deleted["before"] = host_values(host)
            deleted["subnet_id"] = subnet["id"]
            deleted["snapshot"] = deepcopy(host)
            return workspace.delete_host(host_id)

        try:
            return project_mutation(
                action,
                lambda workspace, result: {
                    "action": "host_deleted",
                    "description": f"удалил(а) хост {deleted['ip']}",
                    "before": deleted["before"],
                    "after": {"Состояние": "Удалено"},
                    "undo": {
                        "kind": "host_deleted", "target_id": host_id,
                        "subnet_id": deleted["subnet_id"], "snapshot": deleted["snapshot"],
                    },
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
