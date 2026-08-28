from __future__ import annotations

import ipplan_core
from ipplan_core import Workspace
from project_store import ProjectStore


def test_tree_building_scales_with_prefix_depth_not_subnet_pairs(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path)
    site = {
        "id": "site",
        "cidr": "10.0.0.0/16",
        "name": "Site",
        "values": ["10.0.0.0/16"] + [None] * 16,
        "subnets": [],
    }
    for index in range(128):
        cidr = f"10.0.{index}.0/24"
        site["subnets"].append(
            {
                "id": f"subnet-{index}",
                "cidr": cidr,
                "values": [cidr] + [None] * 16,
                "hosts": [],
            }
        )
    workspace.sites = [site]

    real_parse = ipplan_core.parse_network
    calls = 0

    def counted_parse(value):
        nonlocal calls
        calls += 1
        return real_parse(value)

    monkeypatch.setattr(ipplan_core, "parse_network", counted_parse)
    tree = workspace.tree_for_site(site)

    assert len(tree) == 128
    assert calls < 1000


def test_store_mutation_writes_workspace_once(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path / "data")
    project, _ = store.create_project("P", "1234")

    real_save = Workspace.save
    calls = 0

    def counted_save(workspace):
        nonlocal calls
        calls += 1
        return real_save(workspace)

    monkeypatch.setattr(Workspace, "save", counted_save)
    store.mutate(
        project["id"],
        lambda workspace: workspace.create_site(
            {"name": "Site", "cidr": "10.0.0.0/16"}
        ),
        0,
    )

    assert calls == 1
