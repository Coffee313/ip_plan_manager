from __future__ import annotations

from app import create_app
from project_store import ProjectStore


def register(client, name: str) -> dict:
    return client.post(
        "/api/auth/register",
        json={"login": f"user-{abs(hash(name))}"},
    ).get_json()["data"]


def share_project(client, project_id: str, pin: str, owner: dict, colleague: dict) -> dict:
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
    return {"access_token": ""}


def test_every_successful_change_is_visible_with_actor_and_row_link(tmp_path):
    client = create_app(ProjectStore(tmp_path / "data")).test_client()
    user = register(client, "Анна")
    user_headers = {"X-User-Token": user["access_token"]}
    created = client.post(
        "/api/projects",
        json={"name": "Проект", "pin": "1234"},
        headers=user_headers,
    ).get_json()["data"]
    project_id = created["project"]["id"]
    headers = {
        **user_headers,
        "X-Project-ID": project_id,
        "X-Project-Token": created["access_token"],
        "X-Project-Revision": "0",
    }

    site_response = client.post(
        "/api/sites",
        json={"name": "Москва", "cidr": "10.0.0.0/16"},
        headers=headers,
    )
    assert site_response.status_code == 200
    site_id = site_response.get_json()["data"]["id"]

    logs_response = client.get(
        "/api/audit",
        headers={
            "X-User-Token": user["access_token"],
            "X-Project-ID": project_id,
            "X-Project-Token": created["access_token"],
        },
    )
    assert logs_response.status_code == 200
    events = logs_response.get_json()["data"]
    assert [event["action"] for event in events] == ["site_created", "project_created"]
    assert events[0]["user_name"] == user["user"]["login"]
    assert events[0]["target_type"] == "site"
    assert events[0]["target_id"] == site_id
    assert events[0]["anchor"] == f"site-{site_id}"
    assert events[0]["description"] == "создал(а) площадку «Москва»"

    stale = client.post(
        "/api/sites",
        json={"name": "Не сохранится", "cidr": "10.1.0.0/16"},
        headers=headers,
    )
    assert stale.status_code == 409
    unchanged = client.get(
        "/api/audit",
        headers={
            "X-User-Token": user["access_token"],
            "X-Project-ID": project_id,
            "X-Project-Token": created["access_token"],
        },
    ).get_json()["data"]
    assert len(unchanged) == 2

    updated_headers = {
        **headers,
        "X-Project-Revision": str(site_response.get_json()["revision"]),
    }
    updated = client.put(
        f"/api/sites/{site_id}",
        json={"name": "Москва-2", "cidr": "10.0.0.0/16"},
        headers=updated_headers,
    )
    assert updated.status_code == 200
    newest = client.get(
        "/api/audit",
        headers={
            "X-User-Token": user["access_token"],
            "X-Project-ID": project_id,
            "X-Project-Token": created["access_token"],
        },
    ).get_json()["data"]
    assert newest[0]["action"] == "site_updated"
    assert newest[0]["user_name"] == user["user"]["login"]
    assert newest[0]["before"] == {"Название": "Москва"}
    assert newest[0]["after"] == {"Название": "Москва-2"}
    assert newest[1]["user_name"] == user["user"]["login"]


def test_audit_log_is_available_to_another_user_with_project_access(tmp_path):
    client = create_app(ProjectStore(tmp_path / "data")).test_client()
    owner = register(client, "Владелец")
    colleague = register(client, "Коллега")
    created = client.post(
        "/api/projects",
        json={"name": "Общий", "pin": "4321"},
        headers={"X-User-Token": owner["access_token"]},
    ).get_json()["data"]
    project_id = created["project"]["id"]
    unlocked = share_project(client, project_id, "4321", owner, colleague)

    response = client.get(
        "/api/audit",
        headers={
            "X-User-Token": colleague["access_token"],
            "X-Project-ID": project_id,
            "X-Project-Token": unlocked["access_token"],
        },
    )

    assert response.status_code == 200
    assert response.get_json()["data"][0]["action"] == "project_created"


def test_project_rename_audit_contains_old_and_new_name(tmp_path):
    client = create_app(ProjectStore(tmp_path / "data")).test_client()
    user = register(client, "Редактор")
    created = client.post(
        "/api/projects",
        json={"name": "Старое имя", "pin": "1234"},
        headers={"X-User-Token": user["access_token"]},
    ).get_json()["data"]
    project_id = created["project"]["id"]
    headers = {
        "X-User-Token": user["access_token"],
        "X-Project-ID": project_id,
        "X-Project-Token": created["access_token"],
    }

    response = client.put(
        f"/api/projects/{project_id}",
        json={"name": "Новое имя"},
        headers=headers,
    )

    assert response.status_code == 200
    event = client.get("/api/audit", headers=headers).get_json()["data"][0]
    assert event["action"] == "project_renamed"
    assert event["before"] == {"Название": "Старое имя"}
    assert event["after"] == {"Название": "Новое имя"}


def test_subnet_and_host_changes_are_audited_with_row_links(tmp_path):
    client = create_app(ProjectStore(tmp_path / "data")).test_client()
    user = register(client, "Инженер")
    user_token = user["access_token"]
    created = client.post(
        "/api/projects",
        json={"name": "Сеть", "pin": "1234"},
        headers={"X-User-Token": user_token},
    ).get_json()["data"]
    project_id = created["project"]["id"]
    project_token = created["access_token"]

    def auth(revision=None):
        result = {
            "X-User-Token": user_token,
            "X-Project-ID": project_id,
            "X-Project-Token": project_token,
        }
        if revision is not None:
            result["X-Project-Revision"] = str(revision)
        return result

    site_response = client.post(
        "/api/sites",
        json={"name": "ЦОД", "cidr": "10.20.0.0/16"},
        headers=auth(0),
    )
    site_id = site_response.get_json()["data"]["id"]
    revision = site_response.get_json()["revision"]

    subnet_response = client.post(
        "/api/subnets",
        json={"parent_id": site_id, "cidr": "10.20.1.0/24"},
        headers=auth(revision),
    )
    assert subnet_response.status_code == 200
    subnet_id = subnet_response.get_json()["data"]["id"]
    revision = subnet_response.get_json()["revision"]

    subnet_update = client.put(
        f"/api/subnets/{subnet_id}",
        json={"cidr": "10.20.2.0/24"},
        headers=auth(revision),
    )
    assert subnet_update.status_code == 200
    revision = subnet_update.get_json()["revision"]

    host_response = client.post(
        f"/api/subnets/{subnet_id}/hosts",
        json={"ip": "10.20.2.10", "name": "srv"},
        headers=auth(revision),
    )
    assert host_response.status_code == 200
    host_id = host_response.get_json()["data"]["id"]
    revision = host_response.get_json()["revision"]

    host_update = client.put(
        f"/api/hosts/{host_id}",
        json={"ip": "10.20.2.11", "name": "srv-2"},
        headers=auth(revision),
    )
    assert host_update.status_code == 200
    revision = host_update.get_json()["revision"]

    host_delete = client.delete(
        f"/api/hosts/{host_id}", headers=auth(revision)
    )
    assert host_delete.status_code == 200
    revision = host_delete.get_json()["revision"]

    subnet_delete = client.delete(
        f"/api/subnets/{subnet_id}", headers=auth(revision)
    )
    assert subnet_delete.status_code == 200

    events = client.get("/api/audit", headers=auth()).get_json()["data"]
    by_action = {event["action"]: event for event in events}
    assert by_action["subnet_created"]["anchor"] == f"row-{subnet_id}"
    assert by_action["subnet_updated"]["anchor"] == f"row-{subnet_id}"
    assert by_action["host_created"]["anchor"] == f"row-{host_id}"
    assert by_action["host_updated"]["anchor"] == f"row-{host_id}"
    assert by_action["host_deleted"]["anchor"] == f"row-{subnet_id}"
    assert by_action["subnet_deleted"]["anchor"] == f"site-{site_id}"
    assert by_action["subnet_updated"]["before"] == {"CIDR": "10.20.1.0/24"}
    assert by_action["subnet_updated"]["after"] == {"CIDR": "10.20.2.0/24"}
    assert by_action["host_updated"]["before"] == {
        "IP": "10.20.2.10",
        "Имя": "srv",
    }
    assert by_action["host_updated"]["after"] == {
        "IP": "10.20.2.11",
        "Имя": "srv-2",
    }


def test_audit_write_failure_rolls_back_workspace_and_revision(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path / "data")
    client = create_app(store).test_client()
    user = register(client, "Аварийный тест")
    user_headers = {"X-User-Token": user["access_token"]}
    created = client.post(
        "/api/projects",
        json={"name": "Транзакция", "pin": "1234"},
        headers=user_headers,
    ).get_json()["data"]
    project_id = created["project"]["id"]
    project_headers = {
        **user_headers,
        "X-Project-ID": project_id,
        "X-Project-Token": created["access_token"],
        "X-Project-Revision": "0",
    }

    def fail_audit(*args, **kwargs):
        raise OSError("audit disk error")

    monkeypatch.setattr(store, "_append_audit_unlocked", fail_audit)
    response = client.post(
        "/api/sites",
        json={"name": "Не сохранится", "cidr": "10.77.0.0/16"},
        headers=project_headers,
    )

    assert response.status_code == 400
    state, revision = store.state(project_id)
    assert state["sites"] == []
    assert revision == 0