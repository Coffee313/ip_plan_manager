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
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self.backups_root.mkdir(parents=True, exist_ok=True)
        self.registry_lock = FileLock(str(self.data_root / ".projects.lock"), timeout=15)

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

    def create_project(self, name: str, pin: str) -> tuple[dict[str, Any], str]:
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
            }
            access_token = secrets.token_urlsafe(32)
            meta["access_token_hashes"].append(token_hash(access_token))

            Workspace(project_dir).save()
            self._write_meta_unlocked(project_dir, meta)
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

    def unlock_project(self, project_id: str, pin: str) -> dict[str, Any]:
        pin = validate_pin(pin)
        with self.project_lock(project_id):
            project_dir = self.project_dir(project_id)
            meta = self._read_meta_unlocked(project_dir)

            # Projects from versions without PIN support are claimed on their
            # first unlock inside the trusted company network.
            if not meta.get("pin_hash"):
                meta["pin_hash"] = hash_pin(pin)
            elif not verify_pin(pin, meta["pin_hash"]):
                raise ProjectAccessDenied("Неверный PIN")

            access_token = secrets.token_urlsafe(32)
            hashes = list(meta.get("access_token_hashes", []))
            hashes.append(token_hash(access_token))
            meta["access_token_hashes"] = hashes
            self._write_meta_unlocked(project_dir, meta)
            return {
                "project": self._public_meta(meta),
                "access_token": access_token,
            }

    def rename_project(self, project_id: str, name: str) -> dict[str, Any]:
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
                return self._public_meta(meta)

    def delete_project(self, project_id: str) -> None:
        with self.registry_lock:
            project_dir = self.project_dir(project_id)
            lock = FileLock(str(project_dir / ".lock"), timeout=15)
            with lock:
                trash = self.data_root / ".trash"
                trash.mkdir(exist_ok=True)
                staged = trash / f"{project_id}-{uuid.uuid4().hex}"
                project_dir.rename(staged)
            shutil.rmtree(staged, ignore_errors=True)

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

    def mutate(
        self,
        project_id: str,
        callback: Callable[[Workspace], Any],
        expected_revision: int | None = None,
    ) -> tuple[Any, int]:
        with self.project_lock(project_id):
            project_dir = self.project_dir(project_id)
            meta = self._read_meta_unlocked(project_dir)
            current_revision = int(meta.get("revision", 0))

            if expected_revision is not None and expected_revision != current_revision:
                raise ProjectConflict(current_revision)

            workspace = self._workspace_unlocked(project_dir)
            result = callback(workspace)

            meta["revision"] = current_revision + 1
            meta["updated_at"] = utc_now()
            self._write_meta_unlocked(project_dir, meta)
            return result, meta["revision"]

    def import_excel(
        self,
        project_id: str,
        file_bytes: bytes,
        filename: str,
        expected_revision: int | None = None,
    ) -> tuple[dict[str, Any], int]:
        def action(workspace: Workspace) -> dict[str, Any]:
            workspace.import_file(file_bytes, filename)
            return workspace.state_json()

        return self.mutate(project_id, action, expected_revision)

    def export_excel(self, project_id: str):
        with self.project_lock(project_id):
            project_dir = self.project_dir(project_id)
            workspace = self._workspace_unlocked(project_dir)
            return workspace.export_bytes()
