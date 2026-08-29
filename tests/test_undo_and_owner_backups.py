from __future__ import annotations

from pathlib import Path

from app import create_app
from project_store import ProjectStore


def register(client, name: str) -> dict:
    return client.post("/api/users", json={"name": name}).get_json()["data"]


def project_headers(user: dict, project_id: str, project_token: str, revision=None):
    headers = {
        "X-User-Token": user["access_token"],
        "X-Project-ID": project_id,
        "X-Project-Token": project_token,
    }
    if revision is not None:
        headers["X-Project-Revision"] = str(revision)
    return headers


def test_user_undo_reverts_own_update_without_reverting_colleague(tmp_path):
    store = ProjectStore(tmp_path / "data")
    client = create_app(store).test_client()
    owner = register(client, "Владелец")
    colleague = register(client, "Коллега")
    created = client.post(
        "/api/projects",
        json={"name": "Проект", "pin": "1234"},
        headers={"X-User-Token": owner["access_token"]},
    ).get_json()["data"]
    project_id = created["project"]["id"]
    owner_token = created["access_token"]
    colleague_token = client.post(
        f"/api/projects/{project_id}/unlock",
        json={"pin": "1234"},
        headers={"X-User-Token": colleague["access_token"]},
    ).get_json()["data"]["access_token"]

    first = client.post(
        "/api/sites",
        json={"name": "Своя", "cidr": "10.0.0.0/16"},
        headers=project_headers(owner, project_id, owner_token, 0),
    )
    own_site = first.get_json()["data"]["id"]
    revision = first.get_json()["revision"]
    changed = client.put(
        f"/api/sites/{own_site}",
        json={"name": "Своя новая", "cidr": "10.0.0.0/16"},
        headers=project_headers(owner, project_id, owner_token, revision),
    )
    revision = changed.get_json()["revision"]
    colleague_site = client.post(
        "/api/sites",
        json={"name": "Коллеги", "cidr": "10.1.0.0/16"},
        headers=project_headers(colleague, project_id, colleague_token, revision),
    )
    revision = colleague_site.get_json()["revision"]

    undone = client.post(
        "/api/undo",
        headers=project_headers(owner, project_id, owner_token, revision),
    )

    assert undone.status_code == 200
    state = client.get(
        "/api/state", headers=project_headers(owner, project_id, owner_token)
    ).get_json()["data"]
    assert [site["name"] for site in state["sites"]] == ["Своя", "Коллеги"]


def test_user_undo_refuses_to_overwrite_later_colleague_edit(tmp_path):
    store = ProjectStore(tmp_path / "data")
    client = create_app(store).test_client()
    owner = register(client, "Владелец")
    colleague = register(client, "Коллега")
    created = client.post(
        "/api/projects", json={"name": "Проект", "pin": "1234"},
        headers={"X-User-Token": owner["access_token"]},
    ).get_json()["data"]
    pid = created["project"]["id"]
    colleague_token = client.post(
        f"/api/projects/{pid}/unlock", json={"pin": "1234"},
        headers={"X-User-Token": colleague["access_token"]},
    ).get_json()["data"]["access_token"]
    site = client.post(
        "/api/sites", json={"name": "A", "cidr": "10.0.0.0/16"},
        headers=project_headers(owner, pid, created["access_token"], 0),
    )
    sid = site.get_json()["data"]["id"]
    revision = site.get_json()["revision"]
    own = client.put(
        f"/api/sites/{sid}", json={"name": "B", "cidr": "10.0.0.0/16"},
        headers=project_headers(owner, pid, created["access_token"], revision),
    )
    revision = own.get_json()["revision"]
    later = client.put(
        f"/api/sites/{sid}", json={"name": "C", "cidr": "10.0.0.0/16"},
        headers=project_headers(colleague, pid, colleague_token, revision),
    )

    undone = client.post(
        "/api/undo",
        headers=project_headers(owner, pid, created["access_token"], later.get_json()["revision"]),
    )
    assert undone.status_code == 400
    assert "изменен позже" in undone.get_json()["error"]


def test_user_can_undo_own_new_site(tmp_path):
    store = ProjectStore(tmp_path / "data")
    client = create_app(store).test_client()
    owner = register(client, "Владелец")
    created = client.post(
        "/api/projects", json={"name": "Проект", "pin": "1234"},
        headers={"X-User-Token": owner["access_token"]},
    ).get_json()["data"]
    pid = created["project"]["id"]
    site = client.post(
        "/api/sites", json={"name": "Новая", "cidr": "10.0.0.0/16"},
        headers=project_headers(owner, pid, created["access_token"], 0),
    )

    undone = client.post(
        "/api/undo",
        headers=project_headers(owner, pid, created["access_token"], site.get_json()["revision"]),
    )

    assert undone.status_code == 200
    state, _ = store.state(pid)
    assert state["sites"] == []


def test_user_can_undo_own_deletion_of_colleague_host_and_subnet(tmp_path):
    store = ProjectStore(tmp_path / "data")
    client = create_app(store).test_client()
    owner = register(client, "Владелец")
    colleague = register(client, "Коллега")
    created = client.post(
        "/api/projects", json={"name": "Проект", "pin": "1234"},
        headers={"X-User-Token": owner["access_token"]},
    ).get_json()["data"]
    pid = created["project"]["id"]
    owner_token = created["access_token"]
    colleague_token = client.post(
        f"/api/projects/{pid}/unlock", json={"pin": "1234"},
        headers={"X-User-Token": colleague["access_token"]},
    ).get_json()["data"]["access_token"]
    site = client.post(
        "/api/sites", json={"name": "Площадка", "cidr": "10.0.0.0/16"},
        headers=project_headers(owner, pid, owner_token, 0),
    )
    site_id = site.get_json()["data"]["id"]
    subnet = client.post(
        "/api/subnets", json={"parent_id": site_id, "cidr": "10.0.1.0/24"},
        headers=project_headers(colleague, pid, colleague_token, site.get_json()["revision"]),
    )
    subnet_id = subnet.get_json()["data"]["id"]
    host = client.post(
        f"/api/subnets/{subnet_id}/hosts", json={"ip": "10.0.1.10", "name": "srv"},
        headers=project_headers(colleague, pid, colleague_token, subnet.get_json()["revision"]),
    )
    host_id = host.get_json()["data"]["id"]

    deleted_host = client.delete(
        f"/api/hosts/{host_id}",
        headers=project_headers(owner, pid, owner_token, host.get_json()["revision"]),
    )
    restored_host = client.post(
        "/api/undo",
        headers=project_headers(owner, pid, owner_token, deleted_host.get_json()["revision"]),
    )
    assert restored_host.status_code == 200
    state, revision = store.state(pid)
    assert state["sites"][0]["tree"][0]["hosts"][0]["id"] == host_id

    deleted_subnet = client.delete(
        f"/api/subnets/{subnet_id}",
        headers=project_headers(owner, pid, owner_token, revision),
    )
    restored_subnet = client.post(
        "/api/undo",
        headers=project_headers(owner, pid, owner_token, deleted_subnet.get_json()["revision"]),
    )
    assert restored_subnet.status_code == 200
    state, _ = store.state(pid)
    restored = state["sites"][0]["tree"][0]
    assert restored["id"] == subnet_id
    assert restored["hosts"][0]["id"] == host_id


def test_only_owner_can_create_and_restore_project_backup(tmp_path):
    store = ProjectStore(tmp_path / "data")
    client = create_app(store).test_client()
    owner = register(client, "Владелец")
    colleague = register(client, "Коллега")
    created = client.post(
        "/api/projects", json={"name": "Проект", "pin": "1234"},
        headers={"X-User-Token": owner["access_token"]},
    ).get_json()["data"]
    pid = created["project"]["id"]
    colleague_token = client.post(
        f"/api/projects/{pid}/unlock", json={"pin": "1234"},
        headers={"X-User-Token": colleague["access_token"]},
    ).get_json()["data"]["access_token"]

    denied = client.post(
        f"/api/projects/{pid}/backups",
        headers=project_headers(colleague, pid, colleague_token),
    )
    assert denied.status_code == 403

    made = client.post(
        f"/api/projects/{pid}/backups",
        headers=project_headers(owner, pid, created["access_token"]),
    )
    assert made.status_code == 200
    backup_name = made.get_json()["data"]["filename"]

    current_revision = store.get_revision(pid)
    store.mutate(pid, lambda workspace: workspace.create_site({"name": "После", "cidr": "10.0.0.0/16"}), current_revision)
    restored = client.post(
        f"/api/projects/{pid}/backups/{backup_name}/restore",
        headers=project_headers(owner, pid, created["access_token"]),
    )
    assert restored.status_code == 200
    state, _ = store.state(pid)
    assert state["sites"] == []
    assert store.verify_access(pid, created["access_token"]) is None


def test_deployment_runs_automatic_backup_once_per_day():
    installer = (Path(__file__).resolve().parents[1] / "deploy/install_debian.sh").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 00:00:00" in installer
    assert "Persistent=true" in installer
