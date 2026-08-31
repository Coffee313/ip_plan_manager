from __future__ import annotations

from pathlib import Path

from address_plan_generator import generate_address_plan
from app import create_app
from ipplan_core import Workspace
from project_store import ProjectStore


def sample_payload() -> dict:
    return {
        "sites": [
            {
                "name": "Москва",
                "supernet": "10.10.0.0/16",
                "gateway_mode": "last",
                "vlan_start": 100,
                "vrf": "CORP",
                "zone": "LAN",
                "description_prefix": "MOS",
                "groups": [
                    {"prefix": 24, "count": 2},
                    {"prefix": 26, "count": 2},
                ],
            },
            {
                "name": "Казань",
                "supernet": "10.20.0.0/20",
                "gateway_mode": "first",
                "groups": [{"prefix": 24, "count": 1}],
            },
        ]
    }


def register(client, name: str) -> dict:
    return client.post("/api/users", json={"name": name}).get_json()["data"]


def headers(user: dict, project_id: str, project_token: str, revision: int = 0) -> dict:
    return {
        "X-User-Token": user["access_token"],
        "X-Project-ID": project_id,
        "X-Project-Token": project_token,
        "X-Project-Revision": str(revision),
    }


def test_generator_allocates_multiple_masks_and_sites_deterministically():
    result = generate_address_plan(sample_payload())

    assert [site["name"] for site in result["sites"]] == ["Москва", "Казань"]
    moscow = result["sites"][0]
    assert [item["cidr"] for item in moscow["subnets"]] == [
        "10.10.0.0/24",
        "10.10.1.0/24",
        "10.10.2.0/26",
        "10.10.2.64/26",
    ]
    assert [item["gateway"] for item in moscow["subnets"]] == [
        "10.10.0.254",
        "10.10.1.254",
        "10.10.2.62",
        "10.10.2.126",
    ]
    assert [item["vlan_number"] for item in moscow["subnets"]] == [100, 101, 102, 103]
    assert moscow["subnets"][0]["description"] == "MOS-001"
    assert result["sites"][1]["subnets"][0]["gateway"] == "10.20.0.1"
    assert result["total_subnets"] == 5


def test_generator_rejects_capacity_and_gateway_errors():
    too_small = {
        "sites": [{
            "name": "Малая",
            "supernet": "192.0.2.0/30",
            "gateway_mode": "none",
            "groups": [{"prefix": 31, "count": 3}],
        }]
    }
    try:
        generate_address_plan(too_small)
    except ValueError as error:
        assert "не помещаются" in str(error)
    else:
        raise AssertionError("Ожидалась ошибка емкости")

    point_to_point = {
        "sites": [{
            "name": "P2P",
            "supernet": "192.0.2.0/30",
            "gateway_mode": "first",
            "groups": [{"prefix": 31, "count": 1}],
        }]
    }
    try:
        generate_address_plan(point_to_point)
    except ValueError as error:
        assert "шлюз" in str(error).lower()
    else:
        raise AssertionError("Ожидалась ошибка шлюза")


def test_preview_and_apply_create_one_atomic_audited_plan(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path / "data")
    client = create_app(store).test_client()
    user = register(client, "Архитектор")
    created = client.post(
        "/api/projects",
        json={"name": "Новый план", "pin": "1234"},
        headers={"X-User-Token": user["access_token"]},
    ).get_json()["data"]
    project_id = created["project"]["id"]
    auth = headers(user, project_id, created["access_token"])

    preview = client.post("/api/address-plan/preview", json=sample_payload(), headers=auth)
    assert preview.status_code == 200
    assert preview.get_json()["data"]["total_subnets"] == 5

    real_save = Workspace.save
    save_calls = 0

    def counted_save(workspace):
        nonlocal save_calls
        save_calls += 1
        return real_save(workspace)

    monkeypatch.setattr(Workspace, "save", counted_save)
    applied = client.post("/api/address-plan/apply", json=sample_payload(), headers=auth)
    assert applied.status_code == 200
    assert save_calls == 1
    state, revision = store.state(project_id)
    assert revision == 1
    assert [site["name"] for site in state["sites"]] == ["Москва", "Казань"]
    assert len(state["sites"][0]["tree"]) == 4
    audit = store.audit_log(project_id)
    assert audit[0]["action"] == "address_plan_generated"
    assert audit[0]["user_name"] == "Архитектор"

    undone = client.post(
        "/api/undo",
        headers=headers(user, project_id, created["access_token"], revision),
    )
    assert undone.status_code == 200
    state_after_undo, undo_revision = store.state(project_id)
    assert undo_revision == 2
    assert state_after_undo["sites"] == []


def test_apply_rolls_back_all_sites_when_one_conflicts(tmp_path):
    store = ProjectStore(tmp_path / "data")
    client = create_app(store).test_client()
    user = register(client, "Архитектор")
    created = client.post(
        "/api/projects",
        json={"name": "План", "pin": "1234"},
        headers={"X-User-Token": user["access_token"]},
    ).get_json()["data"]
    project_id = created["project"]["id"]
    auth = headers(user, project_id, created["access_token"])
    existing = client.post(
        "/api/sites",
        json={"name": "Существующая", "cidr": "10.20.0.0/20"},
        headers=auth,
    )
    revision = existing.get_json()["revision"]
    auth["X-Project-Revision"] = str(revision)

    preview = client.post("/api/address-plan/preview", json=sample_payload(), headers=auth)
    assert preview.status_code == 400
    assert "пересекается" in preview.get_json()["error"]

    failed = client.post("/api/address-plan/apply", json=sample_payload(), headers=auth)
    assert failed.status_code == 400
    state, current_revision = store.state(project_id)
    assert current_revision == revision
    assert [site["name"] for site in state["sites"]] == ["Существующая"]


def test_multi_site_generator_ui_is_responsive_and_requires_preview():
    root = Path(__file__).resolve().parents[1]
    html = (root / "templates/index.html").read_text(encoding="utf-8")
    javascript = (root / "static/app.js").read_text(encoding="utf-8")
    css = (root / "static/style.css").read_text(encoding="utf-8")

    for element_id in (
        "generatePlanBtn",
        "generatorDialog",
        "generatorSites",
        "addGeneratorSiteBtn",
        "previewPlanBtn",
        "applyPlanBtn",
    ):
        assert f'id="{element_id}"' in html
    assert "data-generator-site" in javascript
    assert "data-generator-group" in javascript
    assert '"/api/address-plan/preview"' in javascript
    assert '"/api/address-plan/apply"' in javascript
    assert '.generator-site-fields { grid-template-columns: minmax(0, 1fr); }' in css
    assert '.generator-modal form { max-width: 100%; overflow-x: hidden; }' in css
