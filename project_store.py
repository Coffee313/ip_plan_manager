from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock

from ipplan_core import Workspace


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProjectNotFound(ValueError):
    pass


class ProjectConflict(RuntimeError):
    def __init__(self, current_revision: int):
        self.current_revision = current_revision
        super().__init__(
            "Проект уже изменен другим пользователем. "
            "Данные будут обновлены, повторите изменение."
        )


class ProjectAccessDenied(ValueError):
    pass


class UserAccessDenied(ValueError):
    pass


def validate_user_name(name: str) -> str:
    value = " ".join(str(name or "").split())
    if not value:
        raise ValueError("Укажите имя пользователя")
    if len(value) > 80:
        raise ValueError("Имя пользователя слишком длинное")
    return value


def validate_pin(pin: str) -> str:
    value = str(pin or "")
    if len(value) != 4 or not value.isascii() or not value.isdigit():
        raise ValueError("PIN должен содержать ровно четыре цифры")
    return value


def hash_pin(pin: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        validate_pin(pin).encode("ascii"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )
    return "$".join(
        [
            "scrypt",
            "16384",
            "8",
            "1",
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        ]
    )


def verify_pin(pin: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_text, digest_text = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.scrypt(
            validate_pin(pin).encode("ascii"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


class ProjectStore:
    def __init__(self, data_root: str | Path | None = None) -> None:
        configured = data_root or os.environ.get("IP_PLAN_DATA_DIR")
        self.data_root = Path(configured) if configured else Path(__file__).resolve().parent / "data"
        self.projects_root = self.data_root / "projects"
        self.backups_root = self.data_root / "backups"
        self.users_path = self.data_root / "users.json"
        self.system_audit_path = self.data_root / "system_audit.json"
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self.backups_root.mkdir(parents=True, exist_ok=True)
        self.registry_lock = FileLock(str(self.data_root / ".projects.lock"), timeout=15)
        self.users_lock = FileLock(str(self.data_root / ".users.lock"), timeout=15)
        self.system_audit_lock = FileLock(
            str(self.data_root / ".system-audit.lock"), timeout=15
        )

    def _read_users_unlocked(self) -> list[dict[str, Any]]:
        if not self.users_path.exists():
            return []
        payload = json.loads(self.users_path.read_text(encoding="utf-8"))
        users = payload.get("users")
        if not isinstance(users, list):
            raise ValueError("Файл пользователей поврежден")
        return users

    def _write_users_unlocked(self, users: list[dict[str, Any]]) -> None:
        tmp = self.users_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"users": users}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.users_path)

    @staticmethod
    def _public_user(user: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": user["id"],
            "name": user["name"],
            "created_at": user.get("created_at"),
            "updated_at": user.get("updated_at"),
        }

    def create_user(self, name: str) -> tuple[dict[str, Any], str]:
        name = validate_user_name(name)
        access_token = secrets.token_urlsafe(32)
        now = utc_now()
        user = {
            "id": uuid.uuid4().hex,
            "name": name,
            "token_hash": token_hash(access_token),
            "created_at": now,
            "updated_at": now,
        }
        with self.users_lock:
            users = self._read_users_unlocked()
            users.append(user)
            self._write_users_unlocked(users)
        return self._public_user(user), access_token

    def verify_user(self, token: str) -> dict[str, Any]:
        if not token:
            raise UserAccessDenied("Сначала укажите свое имя")
        candidate = token_hash(token)
        with self.users_lock:
            for user in self._read_users_unlocked():
                if hmac.compare_digest(candidate, user.get("token_hash", "")):
                    return self._public_user(user)
        raise UserAccessDenied("Профиль пользователя не найден")

    def update_user(self, token: str, name: str) -> dict[str, Any]:
        name = validate_user_name(name)
        candidate = token_hash(token)
        with self.users_lock:
            users = self._read_users_unlocked()
            for user in users:
                if hmac.compare_digest(candidate, user.get("token_hash", "")):
                    user["name"] = name
                    user["updated_at"] = utc_now()
                    self._write_users_unlocked(users)
                    return self._public_user(user)
        raise UserAccessDenied("Профиль пользователя не найден")

    def _read_system_audit_unlocked(self) -> list[dict[str, Any]]:
        if not self.system_audit_path.exists():
            return []
        payload = json.loads(self.system_audit_path.read_text(encoding="utf-8"))
        events = payload.get("events")
        if not isinstance(events, list):
            raise ValueError("Системный журнал поврежден")
        return events

    def _append_system_audit(self, event: dict[str, Any]) -> None:
        with self.system_audit_lock:
            events = self._read_system_audit_unlocked()
            events.append(event)
            tmp = self.system_audit_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps({"events": events}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.system_audit_path)

    def system_audit_log(self) -> list[dict[str, Any]]:
        with self.system_audit_lock:
            return list(reversed(self._read_system_audit_unlocked()))

    def project_dir(self, project_id: str) -> Path:
        if not project_id or any(ch not in "0123456789abcdef" for ch in project_id.lower()):
            raise ProjectNotFound("Проект не найден")
        path = self.projects_root / project_id
        if not path.is_dir():
            raise ProjectNotFound("Проект не найден")
        return path

    def project_lock(self, project_id: str) -> FileLock:
        return FileLock(str(self.project_dir(project_id) / ".lock"), timeout=15)

    @staticmethod
    def _meta_path(project_dir: Path) -> Path:
        return project_dir / "project.json"

    @staticmethod
    def _audit_path(project_dir: Path) -> Path:
        return project_dir / "audit.json"

    def _read_meta_unlocked(self, project_dir: Path) -> dict[str, Any]:
        path = self._meta_path(project_dir)
        if not path.exists():
            raise ProjectNotFound("Проект не найден")
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_meta_unlocked(self, project_dir: Path, meta: dict[str, Any]) -> None:
        path = self._meta_path(project_dir)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _read_audit_unlocked(self, project_dir: Path) -> list[dict[str, Any]]:
        path = self._audit_path(project_dir)
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        events = payload.get("events")
        if not isinstance(events, list):
            raise ValueError("Журнал проекта поврежден")
        return events

    def _append_audit_unlocked(
        self,
        project_dir: Path,
        actor: dict[str, Any],
        event: dict[str, Any],
    ) -> dict[str, Any]:
        events = self._read_audit_unlocked(project_dir)
        stored = {
            "id": uuid.uuid4().hex,
            "timestamp": utc_now(),
            "user_id": actor["id"],
            "user_name": actor["name"],
            "action": event["action"],
            "description": event["description"],
            "target_type": event["target_type"],
            "target_id": event["target_id"],
            "anchor": event["anchor"],
        }
        events.append(stored)
        path = self._audit_path(project_dir)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"events": events}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
        return stored

    @staticmethod
    def _public_meta(meta: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": meta["id"],
            "name": meta["name"],
            "created_at": meta.get("created_at"),
            "updated_at": meta.get("updated_at"),
            "revision": int(meta.get("revision", 0)),
            "pin_set": bool(meta.get("pin_hash")),
        }

    def _ensure_unique_name(self, name: str, exclude_id: str | None = None) -> None:
        normalized = name.casefold()
        for project in self.list_projects():
            if project["id"] != exclude_id and project["name"].casefold() == normalized:
                raise ValueError(f"Проект «{name}» уже существует")

    def list_projects(self, strict: bool = False) -> list[dict[str, Any]]:
        projects: list[dict[str, Any]] = []
        if not self.projects_root.exists():
            return projects

        for project_dir in self.projects_root.iterdir():
            if not project_dir.is_dir():
                continue
            try:
                meta = self._read_meta_unlocked(project_dir)
                if meta.get("id") != project_dir.name:
                    raise ValueError(
                        f"ID проекта в {project_dir / 'project.json'} не совпадает с каталогом"
                    )
                projects.append(self._public_meta(meta))
            except Exception:
                if strict:
                    raise
                continue

        projects.sort(key=lambda p: (p.get("name", "").casefold(), p.get("created_at", "")))
        return projects

    def create_project(
        self,
        name: str,
        pin: str,
        creator_user_id: str | None = None,
        creator_user_name: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        name = str(name or "").strip()
        if not name:
            raise ValueError("Укажите название проекта")
        if len(name) > 120:
            raise ValueError("Название проекта слишком длинное")
        pin = validate_pin(pin)

        with self.registry_lock:
            self._ensure_unique_name(name)
            project_id = uuid.uuid4().hex
            project_dir = self.projects_root / project_id
            project_dir.mkdir(parents=True, exist_ok=False)

            now = utc_now()
            meta = {
                "id": project_id,
                "name": name,
                "created_at": now,
                "updated_at": now,
                "revision": 0,
                "pin_hash": hash_pin(pin),
                "access_token_hashes": [],
                "creator_user_id": creator_user_id,
            }
            access_token = secrets.token_urlsafe(32)
            meta["access_token_hashes"].append(token_hash(access_token))

            Workspace(project_dir).save()
            self._write_meta_unlocked(project_dir, meta)
            if creator_user_id and creator_user_name:
                self._append_audit_unlocked(
                    project_dir,
                    {"id": creator_user_id, "name": creator_user_name},
                    {
                        "action": "project_created",
                        "description": f"создал(а) проект «{name}»",
                        "target_type": "project",
                        "target_id": project_id,
                        "anchor": "project-root",
                    },
                )
            return self._public_meta(meta), access_token

    def verify_access(self, project_id: str, token: str) -> None:
        if not token:
            raise ProjectAccessDenied("Требуется PIN проекта")
        with self.project_lock(project_id):
            meta = self._read_meta_unlocked(self.project_dir(project_id))
            candidate = token_hash(token)
            if not any(
                hmac.compare_digest(candidate, stored)
                for stored in meta.get("access_token_hashes", [])
            ):
                raise ProjectAccessDenied("Нет доступа к проекту")

    def unlock_project(
        self,
        project_id: str,
        pin: str,
        user_id: str | None = None,
        user_name: str | None = None,
    ) -> dict[str, Any]:
        pin = validate_pin(pin)
        with self.project_lock(project_id):
            project_dir = self.project_dir(project_id)
            meta = self._read_meta_unlocked(project_dir)

            pin_migrated = False
            if not meta.get("pin_hash"):
                if pin != "1111":
                    raise ProjectAccessDenied("Неверный PIN")
                meta["pin_hash"] = hash_pin("1111")
                pin_migrated = True
            if not verify_pin(pin, meta["pin_hash"]):
                raise ProjectAccessDenied("Неверный PIN")

            ownership_claimed = not meta.get("creator_user_id") and bool(user_id)
            if ownership_claimed:
                meta["creator_user_id"] = user_id

            access_token = secrets.token_urlsafe(32)
            hashes = list(meta.get("access_token_hashes", []))
            hashes.append(token_hash(access_token))
            meta["access_token_hashes"] = hashes
            if pin_migrated or ownership_claimed:
                meta["updated_at"] = utc_now()
                meta["revision"] = int(meta.get("revision", 0)) + 1
            self._write_meta_unlocked(project_dir, meta)
            if ownership_claimed and user_id and user_name:
                self._append_audit_unlocked(
                    project_dir,
                    {"id": user_id, "name": user_name},
                    {
                        "action": "project_owner_assigned",
                        "description": "стал(а) владельцем проекта",
                        "target_type": "project",
                        "target_id": project_id,
                        "anchor": "project-root",
                    },
                )
            return {
                "project": self._public_meta(meta),
                "access_token": access_token,
            }

    def set_project_pin(self, project_id: str, pin: str) -> None:
        pin = validate_pin(pin)
        with self.project_lock(project_id):
            project_dir = self.project_dir(project_id)
            meta = self._read_meta_unlocked(project_dir)
            meta["pin_hash"] = hash_pin(pin)
            meta["access_token_hashes"] = []
            meta["updated_at"] = utc_now()
            self._write_meta_unlocked(project_dir, meta)

    def rename_project(
        self, project_id: str, name: str, actor: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        name = str(name or "").strip()
        if not name:
            raise ValueError("Укажите название проекта")
        if len(name) > 120:
            raise ValueError("Название проекта слишком длинное")

        with self.registry_lock:
            self._ensure_unique_name(name, exclude_id=project_id)
            with self.project_lock(project_id):
                project_dir = self.project_dir(project_id)
                meta = self._read_meta_unlocked(project_dir)
                meta["name"] = name
                meta["updated_at"] = utc_now()
                meta["revision"] = int(meta.get("revision", 0)) + 1
                self._write_meta_unlocked(project_dir, meta)
                if actor:
                    self._append_audit_unlocked(
                        project_dir,
                        actor,
                        {
                            "action": "project_renamed",
                            "description": f"переименовал(а) проект в «{name}»",
                            "target_type": "project",
                            "target_id": project_id,
                            "anchor": "project-root",
                        },
                    )
                return self._public_meta(meta)

    def is_creator(self, project_id: str, user_id: str) -> bool:
        return self.get_meta(project_id).get("creator_user_id") == user_id

    def claim_legacy_project_owner(
        self, project_id: str, actor: dict[str, Any]
    ) -> bool:
        """Assign ownership once for projects created before user identities existed."""
        with self.project_lock(project_id):
            project_dir = self.project_dir(project_id)
            meta = self._read_meta_unlocked(project_dir)
            if meta.get("creator_user_id"):
                return False
            meta["creator_user_id"] = actor["id"]
            meta["updated_at"] = utc_now()
            meta["revision"] = int(meta.get("revision", 0)) + 1
            self._write_meta_unlocked(project_dir, meta)
            self._append_audit_unlocked(
                project_dir,
                actor,
                {
                    "action": "project_owner_assigned",
                    "description": "стал(а) владельцем существующего проекта",
                    "target_type": "project",
                    "target_id": project_id,
                    "anchor": "project-root",
                },
            )
            return True

    def delete_project(self, project_id: str, actor: dict[str, Any]) -> None:
        with self.registry_lock:
            project_dir = self.project_dir(project_id)
            lock = FileLock(str(project_dir / ".lock"), timeout=15)
            with lock:
                meta = self._read_meta_unlocked(project_dir)
                if meta.get("creator_user_id") != actor["id"]:
                    raise ProjectAccessDenied(
                        "Удалить проект может только пользователь, который его создал"
                    )
                trash = self.data_root / ".trash"
                trash.mkdir(exist_ok=True)
                staged = trash / f"{project_id}-{uuid.uuid4().hex}"
                project_dir.rename(staged)
                try:
                    self._append_system_audit(
                        {
                            "id": uuid.uuid4().hex,
                            "timestamp": utc_now(),
                            "user_id": actor["id"],
                            "user_name": actor["name"],
                            "action": "project_deleted",
                            "description": f"удалил(а) проект «{meta['name']}»",
                            "project_id": project_id,
                            "target_type": "project",
                            "target_id": project_id,
                            "anchor": "project-root",
                        }
                    )
                except Exception:
                    staged.rename(project_dir)
                    raise
            shutil.rmtree(staged)

    def get_meta(self, project_id: str) -> dict[str, Any]:
        with self.project_lock(project_id):
            return self._read_meta_unlocked(self.project_dir(project_id))

    def get_revision(self, project_id: str) -> int:
        return int(self.get_meta(project_id).get("revision", 0))

    def _workspace_unlocked(self, project_dir: Path) -> Workspace:
        workspace = Workspace(project_dir)
        workspace.load_saved()
        return workspace

    def state(self, project_id: str) -> tuple[dict[str, Any], int]:
        with self.project_lock(project_id):
            project_dir = self.project_dir(project_id)
            meta = self._read_meta_unlocked(project_dir)
            workspace = self._workspace_unlocked(project_dir)
            state = workspace.state_json()
            state["project"] = {
                "id": meta["id"],
                "name": meta["name"],
                "created_at": meta.get("created_at"),
                "updated_at": meta.get("updated_at"),
            }
            revision = int(meta.get("revision", 0))
            state["revision"] = revision
            return state, revision

    def audit_log(self, project_id: str) -> list[dict[str, Any]]:
        with self.project_lock(project_id):
            events = self._read_audit_unlocked(self.project_dir(project_id))
            return list(reversed(events))

    def mutate(
        self,
        project_id: str,
        callback: Callable[[Workspace], Any],
        expected_revision: int | None = None,
        actor: dict[str, Any] | None = None,
        audit_builder: Callable[[Workspace, Any], dict[str, Any]] | None = None,
        snapshot_sources: bool = False,
    ) -> tuple[Any, int]:
        with self.project_lock(project_id):
            project_dir = self.project_dir(project_id)
            meta = self._read_meta_unlocked(project_dir)
            current_revision = int(meta.get("revision", 0))

            if expected_revision is not None and expected_revision != current_revision:
                raise ProjectConflict(current_revision)

            tracked_names = [
                "workspace.json",
                "project.json",
                "audit.json",
            ]
            if snapshot_sources:
                tracked_names.extend(("source.xlsx", "source.xlsm"))
            snapshots = {
                name: (project_dir / name).read_bytes()
                if (project_dir / name).exists()
                else None
                for name in tracked_names
            }

            try:
                workspace = self._workspace_unlocked(project_dir)
                result = callback(workspace)

                if actor and audit_builder:
                    self._append_audit_unlocked(
                        project_dir, actor, audit_builder(workspace, result)
                    )

                meta["revision"] = current_revision + 1
                meta["updated_at"] = utc_now()
                self._write_meta_unlocked(project_dir, meta)
                return result, meta["revision"]
            except Exception:
                for name, content in snapshots.items():
                    path = project_dir / name
                    if content is None:
                        path.unlink(missing_ok=True)
                        continue
                    rollback_tmp = path.with_name(f".{path.name}.rollback.tmp")
                    rollback_tmp.write_bytes(content)
                    rollback_tmp.replace(path)
                for tmp in project_dir.glob("*.tmp"):
                    tmp.unlink(missing_ok=True)
                raise

    def import_excel(
        self,
        project_id: str,
        file_bytes: bytes,
        filename: str,
        expected_revision: int | None = None,
        actor: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], int]:
        def action(workspace: Workspace) -> dict[str, Any]:
            workspace.import_file(file_bytes, filename)
            return workspace.state_json()

        return self.mutate(
            project_id,
            action,
            expected_revision,
            actor=actor,
            audit_builder=(
                (
                    lambda workspace, result: {
                        "action": "excel_imported",
                        "description": f"импортировал(а) Excel «{filename}»",
                        "target_type": "project",
                        "target_id": project_id,
                        "anchor": "project-root",
                    }
                )
                if actor
                else None
            ),
            snapshot_sources=True,
        )

    def export_excel(self, project_id: str):
        with self.project_lock(project_id):
            project_dir = self.project_dir(project_id)
            workspace = self._workspace_unlocked(project_dir)
            return workspace.export_bytes()
