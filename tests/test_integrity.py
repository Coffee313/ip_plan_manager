from __future__ import annotations

import io
from zipfile import BadZipFile

import pytest
from openpyxl import Workbook

from ipplan_core import Workspace, parse_ip, parse_network
from project_store import ProjectStore


def workbook_bytes(site_cidr: str = "10.0.0.0/16") -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "IP Plan"
    sheet.cell(2, 1, site_cidr)
    sheet.cell(2, 9, "Площадка")
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_failed_import_preserves_previous_source_and_export(tmp_path):
    store = ProjectStore(tmp_path / "data")
    project, _ = store.create_project("P", "1234")
    project_id = project["id"]
    store.import_excel(project_id, workbook_bytes(), "plan.xlsx", 0)
    before, _ = store.export_excel(project_id)

    with pytest.raises(BadZipFile):
        store.import_excel(project_id, b"not an xlsx", "plan.xlsx", 1)

    after, _ = store.export_excel(project_id)
    assert after.getvalue() == before.getvalue()
    assert store.get_revision(project_id) == 1


@pytest.mark.parametrize("value", ["2001:db8::/32", "::1/128"])
def test_parse_network_rejects_ipv6(value):
    with pytest.raises(ValueError, match="IPv4"):
        parse_network(value)


@pytest.mark.parametrize("value", ["2001:db8::1", "::1"])
def test_parse_ip_rejects_ipv6(value):
    with pytest.raises(ValueError, match="IPv4"):
        parse_ip(value)


def test_corrupt_workspace_is_reported_instead_of_becoming_empty(tmp_path):
    workspace = Workspace(tmp_path)
    workspace.save()
    workspace.state_file.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="поврежден"):
        workspace.load_saved()
