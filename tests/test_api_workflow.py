from __future__ import annotations

import io

from openpyxl import load_workbook

from app import create_app
from project_store import ProjectStore


def auth_headers(
    project_id: str, token: str, user_token: str, revision: int | None = None
):
    headers = {
        "X-Project-ID": project_id,
        "X-Project-Token": token,
        "X-User-Token": user_token,
    }
    if revision is not None:
        headers["X-Project-Revision"] = str(revision)
    return headers


def test_authenticated_project_round_trip_and_revision_conflict(tmp_path):
    client = create_app(ProjectStore(tmp_path / "data")).test_client()
    user_token = client.post("/api/users", json={"name": "Редактор"}).get_json()[
        "data"
    ]["access_token"]
    created = client.post(
        "/api/projects",
        json={"name": "Проект", "pin": "1234"},
        headers={"X-User-Token": user_token},
    ).get_json()["data"]
    project_id = created["project"]["id"]
    token = created["access_token"]

    state_response = client.get(
        "/api/state", headers=auth_headers(project_id, token, user_token)
    )
    revision = state_response.get_json()["revision"]

    site_response = client.post(
        "/api/sites",
        json={"name": "Москва", "cidr": "10.0.0.0/16"},
        headers=auth_headers(project_id, token, user_token, revision),
    )
    site_id = site_response.get_json()["data"]["id"]
    revision = site_response.get_json()["revision"]

    subnet_response = client.post(
        "/api/subnets",
        json={
            "parent_id": site_id,
            "cidr": "10.0.10.0/24",
            "gateway": "10.0.10.1",
            "vlan_number": "10",
        },
        headers=auth_headers(project_id, token, user_token, revision),
    )
    subnet_id = subnet_response.get_json()["data"]["id"]
    stale_revision = revision
    revision = subnet_response.get_json()["revision"]

    host_response = client.post(
        f"/api/subnets/{subnet_id}/hosts",
        json={"ip": "10.0.10.20", "name": "server"},
        headers=auth_headers(project_id, token, user_token, revision),
    )
    assert host_response.status_code == 200

    conflict = client.post(
        f"/api/subnets/{subnet_id}/hosts",
        json={"ip": "10.0.10.21", "name": "stale"},
        headers=auth_headers(project_id, token, user_token, stale_revision),
    )
    assert conflict.status_code == 409

    assert client.get(
        f"/api/export?project_id={project_id}",
        headers={"X-Project-Token": "wrong"},
    ).status_code == 403
    exported = client.get(
        f"/api/export?project_id={project_id}",
        headers={"X-Project-Token": token},
    )
    assert exported.status_code == 200
    workbook = load_workbook(io.BytesIO(exported.data))
    assert workbook["IP Plan"]["A2"].value == "10.0.0.0/16"


def test_api_allows_same_cidr_in_different_vrfs(tmp_path):
    client = create_app(ProjectStore(tmp_path / "data")).test_client()
    user_token = client.post("/api/users", json={"name": "Редактор"}).get_json()[
        "data"
    ]["access_token"]
    created = client.post(
        "/api/projects",
        json={"name": "Проект", "pin": "1234"},
        headers={"X-User-Token": user_token},
    ).get_json()["data"]
    project_id = created["project"]["id"]
    token = created["access_token"]
    site = client.post(
        "/api/sites",
        json={"name": "Москва", "cidr": "10.0.0.0/16"},
        headers=auth_headers(project_id, token, user_token, 0),
    ).get_json()
    site_id = site["data"]["id"]

    blue = client.post(
        "/api/subnets",
        json={"parent_id": site_id, "cidr": "10.0.10.0/24", "vrf": "BLUE"},
        headers=auth_headers(project_id, token, user_token, site["revision"]),
    )
    red = client.post(
        "/api/subnets",
        json={"parent_id": site_id, "cidr": "10.0.10.0/24", "vrf": "RED"},
        headers=auth_headers(
            project_id, token, user_token, blue.get_json()["revision"]
        ),
    )

    assert blue.status_code == 200
    assert red.status_code == 200
    state = client.get(
        "/api/state", headers=auth_headers(project_id, token, user_token)
    ).get_json()["data"]
    assert [(node["cidr"], node["vrf"]) for node in state["sites"][0]["tree"]] == [
        ("10.0.10.0/24", "BLUE"),
        ("10.0.10.0/24", "RED"),
    ]
