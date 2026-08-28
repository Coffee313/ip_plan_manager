from __future__ import annotations

import json
import zipfile

import pytest

from backup import create_backup
from project_store import ProjectStore
from restore_backup import restore_backup


def project_names(store: ProjectStore) -> list[str]:
    return [project["name"] for project in store.list_projects()]


def test_backup_manifest_matches_archived_projects(tmp_path):
    store = ProjectStore(tmp_path / "data")
    first, _ = store.create_project("A", "1111")
    second, _ = store.create_project("B", "2222")

    backup_path = create_backup(store=store)

    with zipfile.ZipFile(backup_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        archived_ids = {
            name.split("/")[1]
            for name in archive.namelist()
            if name.startswith("projects/") and name.endswith("/project.json")
        }
    assert set(manifest["project_ids"]) == {first["id"], second["id"]}
    assert archived_ids == set(manifest["project_ids"])
    assert manifest["complete"] is True


def test_backup_fails_instead_of_silently_omitting_corrupt_project(tmp_path):
    store = ProjectStore(tmp_path / "data")
    project, _ = store.create_project("P", "1234")
    store.project_dir(project["id"]).joinpath("project.json").write_text(
        "{broken", encoding="utf-8"
    )

    with pytest.raises(json.JSONDecodeError):
        create_backup(store=store)

    assert list(store.backups_root.glob("ipplan-backup-*.zip")) == []


def test_restore_validates_backup_before_replacing_current_projects(tmp_path):
    store = ProjectStore(tmp_path / "data")
    store.create_project("Current", "1234")
    broken = tmp_path / "broken.zip"
    with zipfile.ZipFile(broken, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps({"complete": True, "project_ids": ["deadbeef"]}),
        )
        archive.writestr("projects/deadbeef/project.json", "{}")

    with pytest.raises(ValueError, match="workspace.json"):
        restore_backup(broken, store)

    assert project_names(store) == ["Current"]


def test_restore_replaces_projects_only_after_full_validation(tmp_path):
    source = ProjectStore(tmp_path / "source")
    source.create_project("Restored", "4321")
    backup_path = create_backup(store=source)

    target = ProjectStore(tmp_path / "target")
    target.create_project("Old", "1234")

    restored_count = restore_backup(backup_path, target)

    assert restored_count == 1
    assert project_names(target) == ["Restored"]


def test_restore_rejects_paths_outside_projects_directory(tmp_path):
    store = ProjectStore(tmp_path / "data")
    store.create_project("Current", "1234")
    malicious = tmp_path / "malicious.zip"
    with zipfile.ZipFile(malicious, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps({"complete": True, "project_ids": []}),
        )
        archive.writestr("../escaped.txt", "bad")

    with pytest.raises(ValueError, match="недопустим"):
        restore_backup(malicious, store)
    assert not (tmp_path / "escaped.txt").exists()


def test_backup_can_be_copied_to_external_directory(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path / "data")
    store.create_project("P", "1234")
    external = tmp_path / "external"
    monkeypatch.setenv("IP_PLAN_BACKUP_COPY_DIR", str(external))

    local_path = create_backup(store=store)

    external_path = external / local_path.name
    assert external_path.read_bytes() == local_path.read_bytes()


def test_restore_accepts_legacy_v15_manifest(tmp_path):
    source = ProjectStore(tmp_path / "source")
    project, _ = source.create_project("Legacy", "1234")
    project_dir = source.project_dir(project["id"])
    legacy = tmp_path / "legacy.zip"
    with zipfile.ZipFile(legacy, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "project_count": 1,
                    "projects": [{"id": project["id"], "name": "Legacy"}],
                }
            ),
        )
        for path in project_dir.iterdir():
            if path.is_file() and path.name != ".lock":
                archive.write(path, f"projects/{project['id']}/{path.name}")

    target = ProjectStore(tmp_path / "target")
    assert restore_backup(legacy, target) == 1
    assert project_names(target) == ["Legacy"]
