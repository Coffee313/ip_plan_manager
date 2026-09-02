from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import shutil
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from ipplan_core import Workspace
from project_store import ProjectStore, utc_now


def _validated_members(archive: zipfile.ZipFile) -> tuple[dict, list[zipfile.ZipInfo]]:
    members = archive.infolist()
    for member in members:
        path = PurePosixPath(member.filename)
        if path.is_absolute() or ".." in path.parts or "\\" in member.filename:
            raise ValueError(f"Архив содержит недопустимый путь: {member.filename}")
        if path.parts and path.parts[0] not in {
            "manifest.json",
            "users.json",
            "projects",
        }:
            raise ValueError(f"Архив содержит недопустимый путь: {member.filename}")

    try:
        manifest = json.loads(archive.read("manifest.json"))
    except (KeyError, json.JSONDecodeError) as exc:
        raise ValueError("Некорректный manifest.json") from exc

    if "complete" not in manifest and "project_ids" not in manifest:
        # v15 backups predate the explicit completeness marker. Their manifest
        # listed every intended project, so derive the v16 fields and still run
        # the same archive-content validation below.
        legacy_projects = manifest.get("projects", [])
        if not isinstance(legacy_projects, list):
            raise ValueError("Некорректный legacy manifest.json")
        legacy_ids = [
            project.get("id") for project in legacy_projects if isinstance(project, dict)
        ]
        if len(legacy_ids) != int(manifest.get("project_count", -1)):
            raise ValueError("Некорректный legacy manifest.json")
        manifest["project_ids"] = legacy_ids
        manifest["complete"] = True
        manifest["format_version"] = 1

    if manifest.get("complete") is not True:
        raise ValueError("Нельзя восстановить неполный бэкап")

    users_included = manifest.get("users_included") is True
    names = {member.filename for member in members if not member.is_dir()}
    if users_included:
        if "users.json" not in names:
            raise ValueError("В бэкапе отсутствует users.json")
        users_bytes = archive.read("users.json")
        expected_users_hash = manifest.get("users_sha256")
        actual_users_hash = hashlib.sha256(users_bytes).hexdigest()
        if not isinstance(expected_users_hash, str) or not hmac.compare_digest(
            actual_users_hash, expected_users_hash
        ):
            raise ValueError("Контрольная сумма users.json не совпадает")
        try:
            users_payload = json.loads(users_bytes)
        except json.JSONDecodeError as exc:
            raise ValueError("Некорректный users.json") from exc
        users = users_payload.get("users")
        if not isinstance(users, list) or any(
            not isinstance(user, dict)
            or not isinstance(user.get("id"), str)
            or not isinstance(user.get("name"), str)
            or not (
                isinstance(user.get("token_hash"), str)
                or (
                    isinstance(user.get("login"), str)
                    and isinstance(user.get("password_hash"), str)
                    and isinstance(user.get("session_token_hashes"), list)
                    and all(
                        isinstance(token, str)
                        for token in user.get("session_token_hashes", [])
                    )
                )
            )
            for user in users
        ):
            raise ValueError("Некорректный users.json")

    project_ids = manifest.get("project_ids")
    if not isinstance(project_ids, list) or any(
        not isinstance(project_id, str) for project_id in project_ids
    ):
        raise ValueError("Некорректный список проектов в manifest.json")

    archived_project_ids = {
        parts[1]
        for name in names
        if len(parts := PurePosixPath(name).parts) == 3
        and parts[0] == "projects"
        and parts[2] == "project.json"
    }
    if archived_project_ids != set(project_ids):
        raise ValueError("Состав проектов не совпадает с manifest.json")

    for project_id in project_ids:
        if not project_id or any(ch not in "0123456789abcdef" for ch in project_id.lower()):
            raise ValueError("Некорректный идентификатор проекта в бэкапе")
        project_meta_name = f"projects/{project_id}/project.json"
        workspace_name = f"projects/{project_id}/workspace.json"
        if workspace_name not in names:
            raise ValueError(f"В проекте {project_id} отсутствует workspace.json")
        try:
            project_meta = json.loads(archive.read(project_meta_name))
            workspace = json.loads(archive.read(workspace_name))
        except (KeyError, json.JSONDecodeError) as exc:
            raise ValueError(f"Повреждены данные проекта {project_id}") from exc
        if project_meta.get("id") != project_id:
            raise ValueError(f"ID проекта {project_id} не совпадает с project.json")
        if not isinstance(workspace.get("sites"), list):
            raise ValueError(f"Некорректный workspace.json проекта {project_id}")

    return manifest, members


def restore_backup(backup: Path, store: ProjectStore | None = None) -> int:
    store = store or ProjectStore()
    backup = Path(backup).resolve()
    if not backup.is_file():
        raise ValueError(f"Backup not found: {backup}")

    store.data_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(backup) as archive:
        manifest, members = _validated_members(archive)

        with tempfile.TemporaryDirectory(
            prefix="ipplan-restore-", dir=store.data_root
        ) as tmp:
            staged_root = Path(tmp) / "projects"
            staged_root.mkdir()
            for member in members:
                parts = PurePosixPath(member.filename).parts
                if member.is_dir() or not parts:
                    continue
                if parts[0] == "users.json":
                    destination = Path(tmp) / "users.json"
                    with archive.open(member) as source, destination.open("wb") as target:
                        shutil.copyfileobj(source, target)
                    continue
                if parts[0] != "projects":
                    continue
                destination = Path(tmp).joinpath(*parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)

            rollback = store.data_root / f".projects-rollback-{uuid.uuid4().hex}"
            rollback_users = store.data_root / f".users-rollback-{uuid.uuid4().hex}.json"
            staged_users = Path(tmp) / "users.json"
            with store.registry_lock, store.users_lock:
                moved_old = False
                moved_users = False
                installed_projects = False
                try:
                    if store.projects_root.exists():
                        store.projects_root.rename(rollback)
                        moved_old = True
                    staged_root.rename(store.projects_root)
                    installed_projects = True
                    if store.users_path.exists():
                        store.users_path.rename(rollback_users)
                        moved_users = True
                    if manifest.get("users_included") is True:
                        staged_users.rename(store.users_path)
                except Exception:
                    if store.users_path.exists():
                        store.users_path.unlink()
                    if moved_users and rollback_users.exists():
                        rollback_users.rename(store.users_path)
                    if installed_projects and store.projects_root.exists():
                        shutil.rmtree(store.projects_root)
                    if moved_old and rollback.exists():
                        rollback.rename(store.projects_root)
                    raise
                else:
                    if rollback.exists():
                        shutil.rmtree(rollback)
                    rollback_users.unlink(missing_ok=True)

    return int(manifest.get("project_count", len(manifest["project_ids"])))


def list_project_backups(store: ProjectStore, project_id: str) -> list[dict]:
    available = []
    for path in sorted(store.backups_root.glob("ipplan-backup-*.zip"), reverse=True):
        try:
            with zipfile.ZipFile(path) as archive:
                manifest, _ = _validated_members(archive)
            if project_id not in manifest["project_ids"]:
                continue
            project = next(
                (item for item in manifest.get("projects", []) if item.get("id") == project_id),
                {},
            )
            available.append({
                "filename": path.name,
                "created_at": manifest.get("created_at"),
                "revision": project.get("revision"),
                "size": path.stat().st_size,
            })
        except (OSError, ValueError, zipfile.BadZipFile, KeyError):
            continue
    return available


def restore_project_backup(
    backup: Path, project_id: str, actor: dict, store: ProjectStore
) -> int:
    backup = Path(backup).resolve()
    if backup.parent != store.backups_root.resolve() or not backup.is_file():
        raise ValueError("Бэкап не найден")

    with zipfile.ZipFile(backup) as archive:
        manifest, members = _validated_members(archive)
        if project_id not in manifest["project_ids"]:
            raise ValueError("В этом бэкапе нет выбранного проекта")
        with tempfile.TemporaryDirectory(prefix="ipplan-project-restore-", dir=store.data_root) as tmp:
            staged = Path(tmp) / project_id
            staged.mkdir()
            prefix = ("projects", project_id)
            for member in members:
                parts = PurePosixPath(member.filename).parts
                if member.is_dir() or parts[:2] != prefix:
                    continue
                destination = staged.joinpath(*parts[2:])
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)

            # Fully load the staged workspace before touching the live project.
            Workspace(staged).state_json()
            with store.project_lock(project_id):
                live = store.project_dir(project_id)
                current_meta = store._read_meta_unlocked(live)
                restored_meta = store._read_meta_unlocked(staged)
                for key in (
                    "id", "creator_user_id", "pin_hash", "access_token_hashes",
                    "members", "invite_token_hash",
                ):
                    restored_meta[key] = current_meta.get(key)
                restored_meta["revision"] = int(current_meta.get("revision", 0)) + 1
                restored_meta["updated_at"] = utc_now()
                store._write_meta_unlocked(staged, restored_meta)
                store._append_audit_unlocked(staged, actor, {
                    "action": "project_restored",
                    "description": f"восстановил(а) проект из бэкапа {backup.name}",
                    "target_type": "project", "target_id": project_id,
                    "anchor": "project-root",
                })

                snapshot = {
                    path.relative_to(live): path.read_bytes()
                    for path in live.rglob("*")
                    if path.is_file() and path.name != ".lock"
                }
                try:
                    for path in list(live.rglob("*")):
                        if path.is_file() and path.name != ".lock":
                            path.unlink()
                    for source in staged.rglob("*"):
                        if source.is_file() and source.name != ".lock":
                            destination = live / source.relative_to(staged)
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(source, destination)
                except Exception:
                    for path in list(live.rglob("*")):
                        if path.is_file() and path.name != ".lock":
                            path.unlink()
                    for relative, content in snapshot.items():
                        destination = live / relative
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(content)
                    raise
                return restored_meta["revision"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restore IP Plan Manager projects from a backup ZIP."
    )
    parser.add_argument("backup", type=Path)
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation.",
    )
    args = parser.parse_args()

    store = ProjectStore()
    if not args.yes:
        print("This will replace all current projects in:")
        print(store.projects_root)
        answer = input("Type RESTORE to continue: ")
        if answer != "RESTORE":
            print("Cancelled.")
            return 1

    try:
        count = restore_backup(args.backup, store)
    except Exception as exc:
        print(f"Restore failed: {exc}", file=sys.stderr)
        return 2

    print(f"Restored projects: {count}")
    print(f"Restored from: {args.backup.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
