from __future__ import annotations

from pathlib import Path

from app import create_app
from project_store import ProjectStore


def register(client, name: str) -> dict:
    return client.post(
        "/api/auth/register",
        json={"login": f"user-{abs(hash(name))}"},
    ).get_json()["data"]


def share_project(client, project_id: str, pin: str, owner: dict, colleague: dict) -> str:
    invite = client.post(
        f"/api/projects/{project_id}/invite",
        headers={"X-User-Token": owner["access_token"]},
    ).get_json()["data"]["token"]
    accepted = client.post(
        f"/api/invitations/{invite}/accept",
        json={"pin": pin},
        headers={"X-User-Token": colleague["access_token"]},
    )
    assert accepted.status_code == 200
    return ""


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
    colleague_token = share_project(client, project_id, "1234", owner, colleague)

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
    colleague_token = share_project(client, pid, "1234", owner, colleague)
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
    colleague_token = share_project(client, pid, "1234", owner, colleague)
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


def test_deleted_subnet_can_be_restored_beside_same_addresses_in_other_vrf(tmp_path):
    store = ProjectStore(tmp_path / "data")
    client = create_app(store).test_client()
    owner = register(client, "Владелец")
    created = client.post(
        "/api/projects", json={"name": "Проект", "pin": "1234"},
        headers={"X-User-Token": owner["access_token"]},
    ).get_json()["data"]
    pid = created["project"]["id"]
    token = created["access_token"]
    site = client.post(
        "/api/sites", json={"name": "Площадка", "cidr": "10.0.0.0/16"},
        headers=project_headers(owner, pid, token, 0),
    ).get_json()
    blue = client.post(
        "/api/subnets",
        json={"parent_id": site["data"]["id"], "cidr": "10.0.1.0/24", "vrf": "BLUE"},
        headers=project_headers(owner, pid, token, site["revision"]),
    ).get_json()
    blue_host = client.post(
        f"/api/subnets/{blue['data']['id']}/hosts",
        json={"ip": "10.0.1.10", "name": "blue"},
        headers=project_headers(owner, pid, token, blue["revision"]),
    ).get_json()
    red = client.post(
        "/api/subnets",
        json={"parent_id": site["data"]["id"], "cidr": "10.0.1.0/24", "vrf": "RED"},
        headers=project_headers(owner, pid, token, blue_host["revision"]),
    ).get_json()
    red_host = client.post(
        f"/api/subnets/{red['data']['id']}/hosts",
        json={"ip": "10.0.1.10", "name": "red"},
        headers=project_headers(owner, pid, token, red["revision"]),
    ).get_json()
    deleted = client.delete(
        f"/api/subnets/{blue['data']['id']}",
        headers=project_headers(owner, pid, token, red_host["revision"]),
    ).get_json()

    restored = client.post(
        "/api/undo",
        headers=project_headers(owner, pid, token, deleted["revision"]),
    )

    assert restored.status_code == 200, restored.get_json()
    state, _ = store.state(pid)
    tree = state["sites"][0]["tree"]
    assert [(node["vrf"], node["hosts"][0]["ip"]) for node in tree] == [
        ("BLUE", "10.0.1.10"),
        ("RED", "10.0.1.10"),
    ]


def test_owner_can_undo_selected_colleague_change(tmp_path):
    store = ProjectStore(tmp_path / "data")
    client = create_app(store).test_client()
    owner = register(client, "Владелец")
    colleague = register(client, "Коллега")
    created = client.post(
        "/api/projects", json={"name": "Проект", "pin": "1234"},
        headers={"X-User-Token": owner["access_token"]},
    ).get_json()["data"]
    pid = created["project"]["id"]
    colleague_token = share_project(client, pid, "1234", owner, colleague)
    made = client.post(
        "/api/sites", json={"name": "Коллеги", "cidr": "10.20.0.0/16"},
        headers=project_headers(colleague, pid, colleague_token, 0),
    )
    event_id = store.audit_log(pid)[0]["id"]

    owner_audit = client.get(
        "/api/audit",
        headers=project_headers(owner, pid, created["access_token"]),
    ).get_json()["data"]
    colleague_audit = client.get(
        "/api/audit",
        headers=project_headers(colleague, pid, colleague_token),
    ).get_json()["data"]
    assert owner_audit[0]["can_owner_undo"] is True
    assert colleague_audit[0]["can_owner_undo"] is False

    undone = client.post(
        f"/api/undo/{event_id}",
        headers=project_headers(
            owner, pid, created["access_token"], made.get_json()["revision"]
        ),
    )

    assert undone.status_code == 200
    state, revision = store.state(pid)
    assert revision == 2
    assert state["sites"] == []
    assert f"изменение пользователя {colleague['user']['login']}" in store.audit_log(pid)[0]["description"]
    audit_after_undo = client.get(
        "/api/audit",
        headers=project_headers(owner, pid, created["access_token"]),
    ).get_json()["data"]
    assert all(not event["can_owner_undo"] for event in audit_after_undo)


def test_colleague_cannot_use_owner_undo_endpoint(tmp_path):
    store = ProjectStore(tmp_path / "data")
    client = create_app(store).test_client()
    owner = register(client, "Владелец")
    colleague = register(client, "Коллега")
    created = client.post(
        "/api/projects", json={"name": "Проект", "pin": "1234"},
        headers={"X-User-Token": owner["access_token"]},
    ).get_json()["data"]
    pid = created["project"]["id"]
    colleague_token = share_project(client, pid, "1234", owner, colleague)
    made = client.post(
        "/api/sites", json={"name": "Коллеги", "cidr": "10.20.0.0/16"},
        headers=project_headers(colleague, pid, colleague_token, 0),
    )
    event_id = store.audit_log(pid)[0]["id"]

    denied = client.post(
        f"/api/undo/{event_id}",
        headers=project_headers(
            colleague, pid, colleague_token, made.get_json()["revision"]
        ),
    )

    assert denied.status_code == 403
    state, revision = store.state(pid)
    assert revision == 1
    assert [site["name"] for site in state["sites"]] == ["Коллеги"]


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
    colleague_token = share_project(client, pid, "1234", owner, colleague)

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

    client.post(
        f"/api/projects/{pid}/invite",
        headers={"X-User-Token": owner["access_token"]},
    )
    current_invites = store.get_meta(pid)["invite_token_hashes"]

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
    assert store.get_meta(pid)["invite_token_hashes"] == current_invites


def test_deployment_runs_automatic_backup_once_per_day():
    installer = (Path(__file__).resolve().parents[1] / "deploy/install_debian.sh").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 00:00:00" in installer
    assert "Persistent=true" in installer