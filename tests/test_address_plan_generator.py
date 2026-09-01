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


def k2_cloud_payload() -> dict:
    return {
        "mode": "k2_cloud",
        "k2_cloud": {
            "name": "K2 Cloud",
            "supernet": "172.27.128.0/17",
            "zones": ["COMP", "VOL"],
            "workload_vpcs": [{"name": "VPC INFRA"}],
            "appliance_vpcs": [
                {
                    "name": "VPC FW", "type": "firewall",
                    "zone_scope": "all", "cluster": True,
                },
                {
                    "name": "VPC S2S VPN", "type": "s2s_vpn",
                    "zone_scope": "all", "cluster": False,
                },
                {
                    "name": "VPC RA VPN", "type": "ravpn",
                    "zone_scope": "primary", "cluster": True,
                },
            ],
            "include_transit_vpc": True,
            "transit_vpc_name": "VPC TRANSIT",
        },
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


def test_k2_cloud_generator_builds_vpcs_zones_and_tgw_transit_subnets():
    result = generate_address_plan(k2_cloud_payload())

    assert result["mode"] == "k2_cloud"
    assert result["routing_label"] == "VPC"
    assert result["total_subnets"] == 29
    site = result["sites"][0]
    assert site["name"] == "K2 Cloud"
    assert site["cidr"] == "172.27.128.0/17"

    infra = [item for item in site["subnets"] if item["vrf"] == "VPC INFRA"]
    assert [(item["cidr"], item["zone"], item["description"]) for item in infra] == [
        ("172.27.128.0/22", "COMP, VOL", "Сегмент VPC INFRA"),
        ("172.27.128.0/24", "COMP", "COMP - подсеть виртуальных машин"),
        ("172.27.129.0/28", "COMP", "COMP - транзитная подсеть к TGW"),
        ("172.27.130.0/24", "VOL", "VOL - подсеть виртуальных машин"),
        ("172.27.131.0/28", "VOL", "VOL - транзитная подсеть к TGW"),
    ]

    firewall = [item for item in site["subnets"] if item["vrf"] == "VPC FW"]
    assert [(item["cidr"], item["description"]) for item in firewall[:5]] == [
        ("172.27.136.0/23", "Сегмент VPC FW"),
        ("172.27.136.0/25", "COMP - Firewall outside"),
        ("172.27.136.128/28", "COMP - Firewall inside"),
        ("172.27.136.144/28", "COMP - Firewall interlink"),
        ("172.27.136.160/28", "COMP - транзитная подсеть к TGW"),
    ]

    s2s = [item for item in site["subnets"] if item["vrf"] == "VPC S2S VPN"]
    assert all("interlink" not in item["description"].lower() for item in s2s)
    assert any(item["description"] == "COMP - S2S VPN outside" for item in s2s)
    assert any(item["description"] == "VOL - S2S VPN inside" for item in s2s)

    ravpn = [item for item in site["subnets"] if item["vrf"] == "VPC RA VPN"]
    assert ravpn[0]["cidr"] == "172.27.132.0/22"
    assert ravpn[0]["description"] == "Сегмент пользователей RA VPN"
    assert {item["zone"] for item in ravpn[1:]} == {"COMP"}
    assert any(item["description"] == "COMP - RA VPN interlink" for item in ravpn)

    transit = [item for item in site["subnets"] if item["vrf"] == "VPC TRANSIT"]
    assert [item["cidr"] for item in transit] == [
        "172.27.140.0/23",
        "172.27.140.0/24",
        "172.27.141.0/24",
    ]


def test_k2_cloud_supports_three_zones_and_custom_subnet_masks():
    payload = {
        "mode": "k2_cloud",
        "k2_cloud": {
            "name": "K2 Cloud",
            "supernet": "10.0.0.0/8",
            "zones": ["ZONE-A", "ZONE-B", "ZONE-C"],
            "workload_vpcs": [{
                "name": "VPC VM", "vm_prefix": 25, "tgw_prefix": 29,
            }],
            "appliance_vpcs": [
                {
                    "name": "VPC FW", "type": "firewall", "zone_scope": "all",
                    "cluster": True, "outside_prefix": 26, "inside_prefix": 27,
                    "interlink_prefix": 29, "tgw_prefix": 30,
                },
                {
                    "name": "VPC S2S", "type": "s2s_vpn", "zone_scope": "tertiary",
                    "cluster": False, "outside_prefix": 27, "inside_prefix": 28,
                    "interlink_prefix": 30, "tgw_prefix": 29,
                },
                {
                    "name": "VPC RAVPN", "type": "ravpn", "zone_scope": "all",
                    "cluster": True, "outside_prefix": 27, "inside_prefix": 27,
                    "interlink_prefix": 28, "tgw_prefix": 29, "user_prefix": 20,
                },
            ],
            "include_transit_vpc": True,
            "transit_vpc_name": "VPC TRANSIT",
            "transit_prefix": 25,
        },
    }

    result = generate_address_plan(payload)
    subnets = result["sites"][0]["subnets"]

    workload = [item for item in subnets if item["vrf"] == "VPC VM"][1:]
    assert {item["zone"] for item in workload} == {"ZONE-A", "ZONE-B", "ZONE-C"}
    assert {
        item["cidr"].split("/")[1]
        for item in workload
        if "виртуальных машин" in item["description"]
    } == {"25"}
    assert {
        item["cidr"].split("/")[1]
        for item in workload
        if "TGW" in item["description"]
    } == {"29"}

    expected_appliance_prefixes = {
        "VPC FW": {"outside": "26", "inside": "27", "interlink": "29", "TGW": "30"},
        "VPC S2S": {"outside": "27", "inside": "28", "TGW": "29"},
        "VPC RAVPN": {"outside": "27", "inside": "27", "interlink": "28", "TGW": "29"},
    }
    for vpc, roles in expected_appliance_prefixes.items():
        children = [item for item in subnets if item["vrf"] == vpc][1:]
        for role, prefix in roles.items():
            assert {
                item["cidr"].split("/")[1]
                for item in children
                if role in item["description"]
            } == {prefix}
    s2s = [item for item in subnets if item["vrf"] == "VPC S2S"][1:]
    assert {item["zone"] for item in s2s} == {"ZONE-C"}
    assert not any("interlink" in item["description"] for item in s2s)
    assert [item for item in subnets if item["vrf"] == "VPC RAVPN"][0]["cidr"].endswith("/20")

    transit = [item for item in subnets if item["vrf"] == "VPC TRANSIT"][1:]
    assert {item["zone"] for item in transit} == {"ZONE-A", "ZONE-B", "ZONE-C"}
    assert {item["cidr"].split("/")[1] for item in transit} == {"25"}


def test_k2_cloud_rejects_more_than_three_zones():
    payload = k2_cloud_payload()
    payload["k2_cloud"]["zones"] = ["A", "B", "C", "D"]

    try:
        generate_address_plan(payload)
    except ValueError as error:
        assert "две или три зоны" in str(error)
    else:
        raise AssertionError("Ожидалось ограничение количества зон")


def test_k2_cloud_generator_rejects_duplicate_vpcs_and_insufficient_capacity():
    duplicate = k2_cloud_payload()
    duplicate["k2_cloud"]["appliance_vpcs"][0]["name"] = "VPC INFRA"
    try:
        generate_address_plan(duplicate)
    except ValueError as error:
        assert "VPC INFRA" in str(error) and "повторяется" in str(error)
    else:
        raise AssertionError("Ожидалась ошибка повторяющегося VPC")

    too_small = k2_cloud_payload()
    too_small["k2_cloud"]["supernet"] = "192.0.2.0/24"
    try:
        generate_address_plan(too_small)
    except ValueError as error:
        assert "не помещаются" in str(error)
    else:
        raise AssertionError("Ожидалась ошибка емкости K2 Cloud")


def test_k2_cloud_vpc_limit_includes_optional_transit_vpc():
    payload = {
        "mode": "k2_cloud",
        "k2_cloud": {
            "name": "K2 Cloud",
            "supernet": "10.0.0.0/8",
            "zones": ["COMP", "VOL"],
            "workload_vpcs": [
                {"name": f"VPC VM {index}"}
                for index in range(100)
            ],
            "appliance_vpcs": [],
            "include_transit_vpc": True,
            "transit_vpc_name": "VPC TRANSIT",
        },
    }

    try:
        generate_address_plan(payload)
    except ValueError as error:
        assert "не более 100 VPC" in str(error)
    else:
        raise AssertionError("Ожидалось ограничение общего количества VPC")


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


def test_k2_cloud_preview_and_apply_persist_vpc_hierarchy(tmp_path):
    store = ProjectStore(tmp_path / "data")
    client = create_app(store).test_client()
    user = register(client, "Архитектор")
    created = client.post(
        "/api/projects",
        json={"name": "Облако", "pin": "1234"},
        headers={"X-User-Token": user["access_token"]},
    ).get_json()["data"]
    project_id = created["project"]["id"]
    auth = headers(user, project_id, created["access_token"])

    preview = client.post(
        "/api/address-plan/preview", json=k2_cloud_payload(), headers=auth
    )
    applied = client.post(
        "/api/address-plan/apply", json=k2_cloud_payload(), headers=auth
    )

    assert preview.status_code == 200
    assert preview.get_json()["data"]["routing_label"] == "VPC"
    assert applied.status_code == 200
    state, revision = store.state(project_id)
    assert revision == 1
    roots = state["sites"][0]["tree"]
    assert [node["vrf"] for node in roots] == [
        "VPC INFRA", "VPC RA VPN", "VPC FW", "VPC S2S VPN", "VPC TRANSIT",
    ]
    infra = roots[0]
    assert infra["cidr"] == "172.27.128.0/22"
    assert len(infra["children"]) == 4


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
        "generatorMode",
        "standardGeneratorPanel",
        "k2CloudGeneratorPanel",
        "k2TertiaryZone",
        "generatorSites",
        "addGeneratorSiteBtn",
        "k2WorkloadVpcs",
        "addK2WorkloadVpcBtn",
        "k2ApplianceVpcs",
        "addK2ApplianceVpcBtn",
        "k2IncludeTransit",
        "k2TransitPrefix",
        "previewPlanBtn",
        "applyPlanBtn",
    ):
        assert f'id="{element_id}"' in html
    assert "data-generator-site" in javascript
    assert "data-generator-group" in javascript
    assert "data-k2-workload-vpc" in javascript
    assert "data-k2-appliance-vpc" in javascript
    for field in (
        "data-k2-vm-prefix",
        "data-k2-tgw-prefix",
        "data-k2-outside-prefix",
        "data-k2-inside-prefix",
        "data-k2-interlink-prefix",
        "data-k2-user-prefix",
        'value="tertiary"',
    ):
        assert field in javascript
    assert 'mode: "k2_cloud"' in javascript
    assert "K2 Cloud" in html
    assert "VPC" in html
    assert "TGW" in html
    assert "VRF / VPC" in html
    assert "VRF / VPC" in javascript
    assert '"/api/address-plan/preview"' in javascript
    assert '"/api/address-plan/apply"' in javascript
    assert '.generator-site-fields { grid-template-columns: minmax(0, 1fr); }' in css
    assert '.k2-vpc-row { grid-template-columns: minmax(0, 1fr); }' in css
    assert '.generator-modal form { max-width: 100%; overflow-x: hidden; }' in css
