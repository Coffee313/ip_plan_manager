from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import shutil
import uuid
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock

from ipplan_core import Workspace, subnet_vrf


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


def validate_login(login: str) -> str:
    value = str(login or "").strip().casefold()
    if not value:
        raise ValueError("Укажите корпоративный логин")
    if len(value) > 80:
        raise ValueError("Корпоративный логин слишком длинный")
    if not all(char.isascii() and (char.isalnum() or char in "._@-") for char in value):
        raise ValueError("Логин может содержать латинские буквы, цифры и символы . _ @ -")
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


def data_fingerprint(value: Any) -> str:
    serialized = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


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
            "login": user.get("login"),
            "created_at": user.get("created_at"),
            "updated_at": user.get("updated_at"),
        }

    def create_user(self, login: str) -> tuple[dict[str, Any], str]:
        login = validate_login(login)
        access_token = secrets.token_urlsafe(32)
        now = utc_now()
        user = {
            "id": uuid.uuid4().hex,
            "name": login,
            "login": login,
            "session_token_hashes": [token_hash(access_token)],
            "created_at": now,
            "updated_at": now,
        }
        with self.users_lock:
            users = self._read_users_unlocked()
            if any(existing.get("login", "").casefold() == login for existing in users):
                raise ValueError("Пользователь с таким логином уже зарегистрирован")
            users.append(user)
            self._write_users_unlocked(users)
        return self._public_user(user), access_token

    def login_user(self, login: str) -> tuple[dict[str, Any], str]:
        login = validate_login(login)
        with self.users_lock:
            users = self._read_users_unlocked()
            for user in users:
                if user.get("login", "").casefold() != login:
                    continue
                access_token = secrets.token_urlsafe(32)
                sessions = list(user.get("session_token_hashes", []))
                sessions.append(token_hash(access_token))
                user["name"] = login
                user["session_token_hashes"] = sessions[-20:]
                user["updated_at"] = utc_now()
                user.pop("password_hash", None)
                self._write_users_unlocked(users)
                return self._public_user(user), access_token
        raise UserAccessDenied("Пользователь с таким корпоративным логином не найден")

    def update_user_login(self, token: str, login: str) -> dict[str, Any]:
        login = validate_login(login)
        candidate = token_hash(token)
        with self.users_lock:
            users = self._read_users_unlocked()
            current = None
            for user in users:
                hashes = list(user.get("session_token_hashes", []))
                if user.get("token_hash"):
                    hashes.append(user["token_hash"])
                if any(hmac.compare_digest(candidate, stored) for stored in hashes):
                    current = user
                    break
            if current is None:
                raise UserAccessDenied("Профиль пользователя не найден")
            if any(
                user["id"] != current["id"]
                and user.get("login", "").casefold() == login
                for user in users
            ):
                raise ValueError("Пользователь с таким логином уже зарегистрирован")
            current["login"] = login
            current["name"] = login
            current["updated_at"] = utc_now()
            current.pop("password_hash", None)
            self._write_users_unlocked(users)
            return self._public_user(current)

    def complete_user_registration(
        self, token: str, login: str
    ) -> tuple[dict[str, Any], str]:
        login = validate_login(login)
        candidate = token_hash(token)
        with self.users_lock:
            users = self._read_users_unlocked()
            if any(user.get("login", "").casefold() == login for user in users):
                raise ValueError("Пользователь с таким логином уже зарегистрирован")
            for user in users:
                if not hmac.compare_digest(candidate, user.get("token_hash", "")):
                    continue
                access_token = secrets.token_urlsafe(32)
                user.update(
                    name=login,
                    login=login,
                    session_token_hashes=[token_hash(access_token)],
                    updated_at=utc_now(),
                )
                user.pop("token_hash", None)
                user.pop("password_hash", None)
                self._write_users_unlocked(users)
                return self._public_user(user), access_token
        raise UserAccessDenied("Профиль пользователя не найден")

    def verify_user(self, token: str) -> dict[str, Any]:
        if not token:
            raise UserAccessDenied("Сначала войдите по корпоративному логину")
        candidate = token_hash(token)
        with self.users_lock:
            users = self._read_users_unlocked()
            for user in users:
                hashes = list(user.get("session_token_hashes", []))
                if user.get("token_hash"):
                    hashes.append(user["token_hash"])
                if any(hmac.compare_digest(candidate, stored) for stored in hashes):
                    login = user.get("login")
                    if login and (
                        user.get("name") != login or "password_hash" in user
                    ):
                        user["name"] = login
                        user.pop("password_hash", None)
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
        for key in ("before", "after", "undo"):
            if isinstance(event.get(key), dict) and event[key]:
                stored[key] = event[key]
        if event.get("undoes_event_id"):
            stored["undoes_event_id"] = event["undoes_event_id"]
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

    @staticmethod
    def _member_level(meta: dict[str, Any], user_id: str) -> str | None:
        if meta.get("creator_user_id") == user_id:
            return "owner"
        for member in meta.get("members") or []:
            if member.get("user_id") == user_id:
                return member.get("access_level", "reduced")
        return None

    def access_level(self, project_id: str, user_id: str) -> str | None:
        return self._member_level(self.get_meta(project_id), user_id)

    def require_access(
        self, project_id: str, user_id: str, allowed: set[str] | None = None
    ) -> str:
        level = self.access_level(project_id, user_id)
        if level is None or (allowed is not None and level not in allowed):
            raise ProjectAccessDenied("Нет доступа к проекту")
        return level

    def list_projects_for_user(self, user_id: str) -> list[dict[str, Any]]:
        visible = []
        for project in self.list_projects():
            level = self.access_level(project["id"], user_id)
            if level is None:
                continue
            project["access_level"] = level
            project["can_delete"] = level == "owner"
            project["can_manage_access"] = level == "owner"
            project["can_invite"] = level in {"owner", "full"}
            project["can_manage_project"] = level in {"owner", "full"}
            visible.append(project)
        return visible

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
            # Empty/orphaned directories are not projects. They can remain
            # after an interrupted legacy operation and must not block strict
            # maintenance jobs such as backups.
            if not self._meta_path(project_dir).is_file():
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
                "members": [],
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

    def create_invite(self, project_id: str, actor: dict[str, Any]) -> str:
        self.require_access(project_id, actor["id"], {"owner", "full"})
        invite_token = secrets.token_urlsafe(32)
        with self.project_lock(project_id):
            project_dir = self.project_dir(project_id)
            meta = self._read_meta_unlocked(project_dir)
            invite_hashes = list(meta.get("invite_token_hashes") or [])
            legacy_hash = meta.pop("invite_token_hash", None)
            if legacy_hash:
                invite_hashes.append(legacy_hash)
            invite_hashes.append(token_hash(invite_token))
            meta["invite_token_hashes"] = list(dict.fromkeys(invite_hashes))[-20:]
            meta["updated_at"] = utc_now()
            self._write_meta_unlocked(project_dir, meta)
        return invite_token

    def accept_invite(
        self, invite_token: str, pin: str, actor: dict[str, Any]
    ) -> dict[str, Any]:
        candidate = token_hash(invite_token)
        # An orphaned or incomplete directory must not invalidate invitations
        # for every healthy project in the store.
        for project in self.list_projects():
            with self.project_lock(project["id"]):
                project_dir = self.project_dir(project["id"])
                meta = self._read_meta_unlocked(project_dir)
                stored_hashes = list(meta.get("invite_token_hashes") or [])
                legacy_hash = meta.get("invite_token_hash")
                if legacy_hash:
                    stored_hashes.append(legacy_hash)
                if not any(
                    hmac.compare_digest(candidate, stored)
                    for stored in stored_hashes
                ):
                    continue
                if not verify_pin(pin, meta.get("pin_hash", "")):
                    raise ProjectAccessDenied("Неверный PIN")
                level = self._member_level(meta, actor["id"])
                if level is None:
                    meta.setdefault("members", []).append(
                        {
                            "user_id": actor["id"],
                            "access_level": "reduced",
                            "added_at": utc_now(),
                        }
                    )
                    meta["updated_at"] = utc_now()
                    self._write_meta_unlocked(project_dir, meta)
                    level = "reduced"
                result = self._public_meta(meta)
                result["access_level"] = level
                return result
        raise ProjectNotFound("Ссылка-приглашение недействительна")

    def migrate_legacy_project_access(
        self, tokens: dict[str, str], actor: dict[str, Any]
    ) -> int:
        if not isinstance(tokens, dict) or len(tokens) > 100:
            raise ValueError("Некорректный список старых доступов")
        migrated = 0
        for project_id, access_token in tokens.items():
            if not isinstance(project_id, str) or not isinstance(access_token, str):
                continue
            try:
                project_dir = self.project_dir(project_id)
            except ProjectNotFound:
                continue
            with self.project_lock(project_id):
                meta = self._read_meta_unlocked(project_dir)
                candidate = token_hash(access_token)
                stored_tokens = meta.get("access_token_hashes", [])
                if not any(hmac.compare_digest(candidate, stored) for stored in stored_tokens):
                    continue
                meta["access_token_hashes"] = [
                    stored
                    for stored in stored_tokens
                    if not hmac.compare_digest(candidate, stored)
                ]
                if self._member_level(meta, actor["id"]) is not None:
                    self._write_meta_unlocked(project_dir, meta)
                    continue
                if not meta.get("creator_user_id"):
                    meta["creator_user_id"] = actor["id"]
                else:
                    meta.setdefault("members", []).append(
                        {
                            "user_id": actor["id"],
                            "access_level": "reduced",
                            "added_at": utc_now(),
                        }
                    )
                meta["updated_at"] = utc_now()
                self._write_meta_unlocked(project_dir, meta)
                migrated += 1
        return migrated

    def list_members(self, project_id: str, actor: dict[str, Any]) -> list[dict[str, Any]]:
        self.require_access(project_id, actor["id"], {"owner"})
        meta = self.get_meta(project_id)
        levels = {meta.get("creator_user_id"): "owner"}
        levels.update(
            {
                member.get("user_id"): member.get("access_level", "reduced")
                for member in meta.get("members") or []
            }
        )
        with self.users_lock:
            users = self._read_users_unlocked()
        result = []
        for user in users:
            if user["id"] not in levels:
                continue
            public = self._public_user(user)
            public["access_level"] = levels[user["id"]]
            result.append(public)
        result.sort(key=lambda member: (member["access_level"] != "owner", member["name"].casefold()))
        return result

    def set_member_access(
        self, project_id: str, user_id: str, access_level: str, actor: dict[str, Any]
    ) -> dict[str, Any]:
        self.require_access(project_id, actor["id"], {"owner"})
        if access_level not in {"full", "reduced"}:
            raise ValueError("Уровень доступа должен быть full или reduced")
        with self.project_lock(project_id):
            project_dir = self.project_dir(project_id)
            meta = self._read_meta_unlocked(project_dir)
            if meta.get("creator_user_id") == user_id:
                raise ValueError("Нельзя изменить права владельца")
            member = next(
                (item for item in meta.get("members") or [] if item.get("user_id") == user_id),
                None,
            )
            if member is None:
                raise ValueError("Пользователь не имеет доступа к проекту")
            member["access_level"] = access_level
            meta["updated_at"] = utc_now()
            self._write_meta_unlocked(project_dir, meta)
        return {"id": user_id, "access_level": access_level}

    def remove_member(self, project_id: str, user_id: str, actor: dict[str, Any]) -> None:
        self.require_access(project_id, actor["id"], {"owner"})
        with self.project_lock(project_id):
            project_dir = self.project_dir(project_id)
            meta = self._read_meta_unlocked(project_dir)
            if meta.get("creator_user_id") == user_id:
                raise ValueError("Нельзя удалить владельца проекта")
            members = list(meta.get("members") or [])
            filtered = [member for member in members if member.get("user_id") != user_id]
            if len(filtered) == len(members):
                raise ValueError("Пользователь не имеет доступа к проекту")
            meta["members"] = filtered
            meta["updated_at"] = utc_now()
            self._write_meta_unlocked(project_dir, meta)

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

            if meta.get("creator_user_id"):
                raise ProjectAccessDenied(
                    "Для подключения к проекту используйте ссылку-приглашение"
                )

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

            if user_id and not ownership_claimed and self._member_level(meta, user_id) is None:
                meta.setdefault("members", []).append(
                    {
                        "user_id": user_id,
                        "access_level": "reduced",
                        "added_at": utc_now(),
                    }
                )

            access_token = secrets.token_urlsafe(32)
            hashes = list(meta.get("access_token_hashes", []))
            hashes.append(token_hash(access_token))
            meta["access_token_hashes"] = hashes
            if pin_migrated or ownership_claimed or user_id:
                meta["updated_at"] = utc_now()
                if pin_migrated or ownership_claimed:
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
            with self.project_lock(project_id):
                project_dir = self.project_dir(project_id)
                meta = self._read_meta_unlocked(project_dir)
                if not actor or self._member_level(meta, actor.get("id", "")) not in {"owner", "full"}:
                    raise ProjectAccessDenied(
                        "Переименовать проект может владелец или пользователь с полным доступом"
                    )
                self._ensure_unique_name(name, exclude_id=project_id)
                old_name = meta["name"]
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
                            "description": "переименовал(а) проект",
                            "before": {"Название": old_name},
                            "after": {"Название": name},
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

    @staticmethod
    def _undo_values(workspace: Workspace, kind: str, target_id: str) -> dict[str, Any]:
        base_kind = kind.removesuffix("_created")
        if base_kind == "site":
            site = workspace.find_site(target_id)
            return {"Название": site["name"], "CIDR": site["cidr"]}
        if base_kind == "subnet":
            subnet = workspace.find_subnet(target_id)[1]
            values = list(subnet.get("values") or []) + [None] * 17
            return {
                "CIDR": subnet["cidr"], "Gateway": values[1] or "", "VRF": values[2] or "",
                "VLAN": values[3] if values[3] is not None else "", "VLAN Name": values[4] or "",
                "Комментарий": values[5] or "", "Зона": values[6] or "",
                "Площадка": values[7] or "", "Описание": values[8] or "",
            }
        if base_kind == "host":
            host = workspace.find_host(target_id)[2]
            values = list(host.get("values") or []) + [None] * 17
            return {
                "IP": values[0] or "", "Имя": values[10] or "", "Роль": values[11] or "",
                "Подсистема": values[9] or "", "CPU": values[12] or "", "RAM": values[13] or "",
                "Диск": values[14] or "", "Тип": values[15] or "", "Статус": values[16] or "",
                "Комментарий": values[5] or "",
            }
        raise ValueError("Это изменение нельзя отменить")

    @staticmethod
    def _apply_undo_values(workspace: Workspace, kind: str, target_id: str, values: dict[str, Any]) -> Any:
        if kind == "site_created":
            site = workspace.find_site(target_id)
            if site.get("subnets"):
                raise ValueError("Площадка изменена позже; отмена затронула бы изменения коллеги")
            return workspace.delete_site(target_id)
        if kind == "subnet_created":
            site, subnet = workspace.find_subnet(target_id)
            target_network = ipaddress.ip_network(subnet["cidr"], strict=False)
            target_vrf = subnet_vrf(subnet)
            has_children = any(
                other["id"] != target_id
                and subnet_vrf(other) == target_vrf
                and ipaddress.ip_network(other["cidr"], strict=False).subnet_of(target_network)
                for other in site.get("subnets", [])
            )
            if subnet.get("hosts") or has_children:
                raise ValueError("Подсеть изменена позже; отмена затронула бы изменения коллеги")
            return workspace.delete_subnet(target_id)
        if kind == "host_created":
            return workspace.delete_host(target_id)
        if kind == "host_deleted":
            site, subnet = workspace.find_subnet(values["subnet_id"])
            snapshot = deepcopy(values["snapshot"])
            restored_ip = ipaddress.ip_address(snapshot["values"][0])
            target_vrf = subnet_vrf(subnet)
            best = workspace.most_specific_subnet_for_ip(site, restored_ip, target_vrf)
            if best is None or best["id"] != subnet["id"]:
                raise ValueError("Структура подсетей изменена позже; хост нельзя безопасно восстановить")
            for current_subnet in site["subnets"]:
                if subnet_vrf(current_subnet) != target_vrf:
                    continue
                for host in current_subnet.get("hosts", []):
                    if host["id"] == target_id or str(host["values"][0]) == str(restored_ip):
                        raise ValueError("IP хоста уже используется; восстановление отменено")
            subnet.setdefault("hosts", []).append(snapshot)
            subnet["hosts"].sort(key=lambda host: int(ipaddress.ip_address(host["values"][0])))
            workspace.save()
            return {"restored": target_id}
        if kind == "subnet_deleted":
            site = workspace.find_site(values["site_id"])
            snapshots = deepcopy(values["snapshots"])
            existing_ids = {subnet["id"] for subnet in site["subnets"]}
            restored_ids = {subnet["id"] for subnet in snapshots}
            if existing_ids & restored_ids:
                raise ValueError("Одна из удаленных подсетей уже существует")
            for subnet in snapshots:
                workspace.validate_subnet_network(
                    site,
                    ipaddress.ip_network(subnet["cidr"], strict=False),
                    subnet_vrf(subnet),
                )

            existing_host_ids = {
                host["id"] for subnet in site["subnets"] for host in subnet.get("hosts", [])
            }
            existing_ips = {
                (subnet_vrf(subnet), str(host["values"][0]))
                for subnet in site["subnets"] for host in subnet.get("hosts", [])
            }
            prospective = site["subnets"] + snapshots
            for subnet in snapshots:
                target_vrf = subnet_vrf(subnet)
                for host in subnet.get("hosts", []):
                    host_ip = ipaddress.ip_address(host["values"][0])
                    if (
                        host["id"] in existing_host_ids
                        or (target_vrf, str(host_ip)) in existing_ips
                    ):
                        raise ValueError("Данные хоста уже используются; восстановление отменено")
                    matching = [
                        candidate for candidate in prospective
                        if subnet_vrf(candidate) == target_vrf
                        if host_ip in ipaddress.ip_network(candidate["cidr"], strict=False)
                    ]
                    best = max(
                        matching,
                        key=lambda candidate: ipaddress.ip_network(candidate["cidr"], strict=False).prefixlen,
                    )
                    if best["id"] != subnet["id"]:
                        raise ValueError("Структура подсетей изменена позже; восстановление небезопасно")
            site["subnets"].extend(snapshots)
            site["subnets"].sort(
                key=lambda subnet: (
                    int(ipaddress.ip_network(subnet["cidr"], strict=False).network_address),
                    ipaddress.ip_network(subnet["cidr"], strict=False).prefixlen,
                    subnet_vrf(subnet),
                )
            )
            workspace.save()
            return {"restored": [subnet["id"] for subnet in snapshots]}
        if kind == "site":
            return workspace.update_site(target_id, {"name": values["Название"], "cidr": values["CIDR"]})
        if kind == "subnet":
            return workspace.update_subnet(target_id, {
                "cidr": values["CIDR"], "gateway": values["Gateway"], "vrf": values["VRF"],
                "vlan_number": values["VLAN"], "vlan_name": values["VLAN Name"],
                "comment": values["Комментарий"], "zone": values["Зона"],
                "site": values["Площадка"], "description": values["Описание"],
            })
        if kind == "host":
            return workspace.update_host(target_id, {
                "ip": values["IP"], "name": values["Имя"], "role": values["Роль"],
                "subsystem": values["Подсистема"], "cpu": values["CPU"], "ram": values["RAM"],
                "disk": values["Диск"], "type": values["Тип"], "status": values["Статус"],
                "comment": values["Комментарий"],
            })
        raise ValueError("Это изменение нельзя отменить")

    def _undo_change(
        self,
        project_id: str,
        actor: dict[str, Any],
        expected_revision: int | None = None,
        selected_event_id: str | None = None,
    ) -> tuple[dict[str, Any], int]:
        events = self.audit_log(project_id)
        undone = {event.get("undoes_event_id") for event in events if event.get("undoes_event_id")}
        if selected_event_id is None:
            original = next((
                event for event in events
                if event.get("user_id") == actor["id"]
                and event.get("action") != "change_undone"
                and event.get("id") not in undone
            ), None)
        else:
            original = next((
                event for event in events
                if event.get("id") == selected_event_id
                and event.get("action") != "change_undone"
                and event.get("id") not in undone
            ), None)
        if original is None:
            if selected_event_id is None:
                raise ValueError("Нет ваших изменений, которые можно отменить")
            raise ValueError("Изменение не найдено или уже отменено")
        if not isinstance(original.get("undo"), dict):
            raise ValueError("Это изменение нельзя безопасно отменить автоматически")
        undo = original["undo"]
        kind, target_id = undo["kind"], undo["target_id"]

        def action(workspace: Workspace) -> dict[str, Any]:
            if kind == "address_plan_generated":
                generated_sites = undo.get("sites")
                if not isinstance(generated_sites, list) or not generated_sites:
                    raise ValueError("Некорректные данные отмены генерации")
                for generated_site in generated_sites:
                    site = workspace.find_site(generated_site["id"])
                    if data_fingerprint(site) != generated_site["fingerprint"]:
                        raise ValueError(
                            "Сгенерированный план изменен позже; отмена затронула бы "
                            "изменения коллеги"
                        )
                generated_ids = {site["id"] for site in generated_sites}
                workspace.sites = [
                    site for site in workspace.sites if site["id"] not in generated_ids
                ]
                workspace.save()
                return {"event_id": original["id"], "target_id": target_id}
            if kind.endswith("_deleted"):
                self._apply_undo_values(workspace, kind, target_id, undo)
            else:
                if self._undo_values(workspace, kind, target_id) != undo["after"]:
                    raise ValueError("Объект изменен позже; отмена затронула бы изменения коллеги")
                self._apply_undo_values(workspace, kind, target_id, undo["before"])
            return {"event_id": original["id"], "target_id": target_id}

        own_change = original.get("user_id") == actor["id"]
        undo_description = (
            f"отменил(а) свое изменение: {original['description']}"
            if own_change
            else (
                f"отменил(а) изменение пользователя "
                f"{original.get('user_name') or 'Неизвестный пользователь'}: "
                f"{original['description']}"
            )
        )
        return self.mutate(project_id, action, expected_revision, actor=actor, audit_builder=lambda workspace, result: {
            "action": "change_undone",
            "description": undo_description,
            "before": original.get("after", {}), "after": original.get("before", {}),
            "target_type": original["target_type"], "target_id": target_id,
            "anchor": original["anchor"], "undoes_event_id": original["id"],
        })

    def undo_last_change(
        self,
        project_id: str,
        actor: dict[str, Any],
        expected_revision: int | None = None,
    ) -> tuple[dict[str, Any], int]:
        return self._undo_change(project_id, actor, expected_revision)

    def undo_event_as_owner(
        self,
        project_id: str,
        actor: dict[str, Any],
        event_id: str,
        expected_revision: int | None = None,
    ) -> tuple[dict[str, Any], int]:
        if not self.is_creator(project_id, actor["id"]):
            raise ProjectAccessDenied(
                "Только владелец проекта может отменять изменения коллег"
            )
        return self._undo_change(
            project_id, actor, expected_revision, selected_event_id=event_id
        )

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
