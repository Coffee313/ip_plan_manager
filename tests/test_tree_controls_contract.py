from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_subnets_are_collapsed_by_default_and_collapse_all_control_exists():
    html = (ROOT / "templates/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "static/app.js").read_text(encoding="utf-8")

    assert 'id="collapseAllBtn"' in html
    assert "function collapseAllSubnets" in javascript
    assert '$("collapseAllBtn").onclick = collapseAllSubnets' in javascript
    assert "collapseAllSubnets();" in javascript


def test_deleted_audit_events_use_distinct_location_highlight():
    javascript = (ROOT / "static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "static/style.css").read_text(encoding="utf-8")

    assert "focusAuditTarget(anchor, event.action)" in javascript
    assert "audit-deletion-target-flash" in javascript
    assert ".audit-deletion-target-flash" in css


def test_header_uses_user_avatar_and_popup_menu_without_global_add_subnet():
    html = (ROOT / "templates/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "static/style.css").read_text(encoding="utf-8")

    assert 'id="addSubnetBtn"' not in html
    assert 'id="userAvatar"' in html
    assert 'id="headerMenuBtn"' in html
    assert 'id="headerMenu"' in html
    assert 'id="undoBtn"' in html
    assert 'id="backupsBtn"' in html
    assert 'id="templateBtn"' in html
    assert "userInitial(currentUser?.name)" in javascript
    assert "function toggleHeaderMenu" in javascript
    assert '$("renameProjectBtn").disabled = !hasAccess || !project?.can_delete' in javascript
    assert "const canImport = hasAccess && !!project?.can_delete" in javascript
    assert ".header-menu" in css


def test_project_choice_uses_tiles_and_row_buttons_are_in_russian():
    javascript = (ROOT / "static/app.js").read_text(encoding="utf-8")

    assert 'class="project-tile"' in javascript
    assert "Проект не открыт" not in javascript
    assert "+ subnet" not in javascript
    assert "+ host" not in javascript
    assert ">Actions<" not in javascript


def test_audit_renderer_displays_before_and_after_values():
    javascript = (ROOT / "static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "static/style.css").read_text(encoding="utf-8")

    assert 'beforeLabel.textContent = "До"' in javascript
    assert 'afterLabel.textContent = "После"' in javascript
    assert ".audit-change" in css
