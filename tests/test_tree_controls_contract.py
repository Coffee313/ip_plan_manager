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
    assert "userInitial(currentUser?.login)" in javascript
    assert "function toggleHeaderMenu" in javascript
    assert '$("renameProjectBtn").disabled = !hasAccess || !project?.can_manage_project' in javascript
    assert "const canImport = hasAccess && !!project?.can_manage_project" in javascript
    assert '${currentProject()?.can_manage_project ? `<label class="btn secondary">' in javascript
    assert 'if (!currentProject()?.can_manage_project) return;' in javascript
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


def test_undo_is_a_prominent_corner_action_with_safe_ctrl_z_hotkey():
    html = (ROOT / "templates/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "static/style.css").read_text(encoding="utf-8")

    assert 'id="undoBtn" class="undo-fab"' in html
    assert "Ctrl+Z" in html
    assert "Ctrl+Я" in html
    assert html.index('id="undoBtn"') > html.index('<div class="app-layout">')
    assert ".undo-fab" in css
    assert "position: fixed" in css
    assert "right: 18px" in css and "bottom: 18px" in css
    assert "function handleUndoHotkey(event)" in javascript
    assert 'event.code === "KeyZ"' in javascript
    assert '["z", "я"].includes(event.key.toLowerCase())' in javascript
    assert "isEditableUndoTarget(event.target)" in javascript
    assert 'document.querySelector("dialog[open]")' in javascript
    assert "event.repeat" in javascript


def test_application_has_an_svg_favicon():
    html = (ROOT / "templates/index.html").read_text(encoding="utf-8")
    favicon = ROOT / "static/favicon.svg"

    assert '<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">' in html
    assert favicon.exists()
    assert "<svg" in favicon.read_text(encoding="utf-8")


def test_owner_can_undo_selected_audit_event_from_journal():
    javascript = (ROOT / "static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "static/style.css").read_text(encoding="utf-8")

    assert "event.can_owner_undo" in javascript
    assert "async function undoAuditEvent(event)" in javascript
    assert "`/api/undo/${encodeURIComponent(event.id)}`" in javascript
    assert "audit-owner-undo" in javascript
    assert ".audit-owner-undo" in css
