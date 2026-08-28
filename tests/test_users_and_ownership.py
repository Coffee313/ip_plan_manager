from __future__ import annotations

from app import create_app
from project_store import ProjectStore


def register(client, name: str) -> dict:
    response = client.post("/api/users", json={"name": name})
    assert response.status_code == 200
    return response.get_json()["data"]


def user_headers(token: str) -> dict[str, str]:
    return {"X-User-Token": token}


def test_user_registration_profile_edit_and_restart_persistence(tmp_path):
    data_root = tmp_path / "data"
    client = create_app(ProjectStore(data_root)).test_client()

    registered = register(client, "Анна")
    token = registered["access_token"]
    assert registered["user"]["name"] == "Анна"

    updated = client.put(
        "/api/users/me", json={"name": "Анна Петрова"}, headers=user_headers(token)
    )
    assert updated.status_code == 200
    assert updated.get_json()["data"]["name"] == "Анна Петрова"

    restarted = create_app(ProjectStore(data_root)).test_client()
    me = restarted.get("/api/users/me", headers=user_headers(token))
    assert me.status_code == 200
    assert me.get_json()["data"]["name"] == "Анна Петрова"


def test_only_creator_can_delete_project(tmp_path):
    client = create_app(ProjectStore(tmp_path / "data")).test_client()
    owner = register(client, "Владелец")
    colleague = register(client, "Коллега")

    anonymous_create = client.post(
        "/api/projects", json={"name": "Без владельца", "pin": "1234"}
    )
    assert anonymous_create.status_code == 401

    created = client.post(
        "/api/projects",
        json={"name": "Проект", "pin": "1234"},
        headers=user_headers(owner["access_token"]),
    ).get_json()["data"]
    project_id = created["project"]["id"]
    owner_project_token = created["access_token"]

    owner_list = client.get(
        "/api/projects", headers=user_headers(owner["access_token"])
    ).get_json()["data"]
    colleague_list = client.get(
        "/api/projects", headers=user_headers(colleague["access_token"])
    ).get_json()["data"]
    assert owner_list[0]["can_delete"] is True
    assert colleague_list[0]["can_delete"] is False

    unlocked = client.post(
        f"/api/projects/{project_id}/unlock",
        json={"pin": "1234"},
        headers=user_headers(colleague["access_token"]),
    ).get_json()["data"]
    colleague_headers = {
        "X-User-Token": colleague["access_token"],
        "X-Project-Token": unlocked["access_token"],
    }
    denied = client.delete(f"/api/projects/{project_id}", headers=colleague_headers)
    assert denied.status_code == 403

    owner_headers = {
        "X-User-Token": owner["access_token"],
        "X-Project-Token": owner_project_token,
    }
    deleted = client.delete(f"/api/projects/{project_id}", headers=owner_headers)
    assert deleted.status_code == 200
