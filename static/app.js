let state = null;
let projects = [];
const userTokenStorageKey = "ipPlanManager.userToken";
let userToken = localStorage.getItem(userTokenStorageKey) || "";
let currentUser = null;
let userDialogResolve = null;
let currentProjectId = localStorage.getItem("ipPlanManager.projectId") || "";
const projectTokensStorageKey = "ipPlanManager.projectTokens";
let projectTokens = {};
try {
  projectTokens = JSON.parse(localStorage.getItem(projectTokensStorageKey) || "{}") || {};
} catch (_) {
  projectTokens = {};
}
let projectRevision = null;
let pendingRemoteRevision = null;
let projectDialogMode = "create";

let subnetEditId = null;
let hostEditId = null;
let hostSubnetId = null;
let hostFixedPrefix = "";
let hostAddressFullyFixed = false;
let hostInfoEditId = null;
const collapsed = new Set();

const $ = (id) => document.getElementById(id);

function saveProjectTokens() {
  localStorage.setItem(projectTokensStorageKey, JSON.stringify(projectTokens));
}

function currentProjectToken() {
  return projectTokens[currentProjectId] || "";
}

function forgetProjectToken(projectId) {
  delete projectTokens[projectId];
  saveProjectTokens();
}

function requestNeedsProjectAccess(url) {
  return !url.startsWith("/api/users") && url !== "/api/projects" && !url.endsWith("/unlock");
}

function isMutation(options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  return !["GET", "HEAD", "OPTIONS"].includes(method);
}

async function api(url, options = {}) {
  const fetchOptions = {...options};
  const headers = new Headers(options.headers || {});

  if (userToken) {
    headers.set("X-User-Token", userToken);
  }

  if (currentProjectId && requestNeedsProjectAccess(url)) {
    headers.set("X-Project-ID", currentProjectId);
    if (currentProjectToken()) {
      headers.set("X-Project-Token", currentProjectToken());
    }
    if (isMutation(options) && projectRevision !== null) {
      headers.set("X-Project-Revision", String(projectRevision));
    }
  }

  fetchOptions.headers = headers;
  const res = await fetch(url, fetchOptions);

  let body = null;
  try { body = await res.json(); } catch (_) {}

  if (!res.ok || !body?.ok) {
    if (res.status === 401) {
      userToken = "";
      currentUser = null;
      localStorage.removeItem(userTokenStorageKey);
    }
    if (
      res.status === 403 &&
      currentProjectId &&
      /(?:Нет доступа к проекту|Требуется PIN проекта)/.test(body?.error || "")
    ) {
      forgetProjectToken(currentProjectId);
    }
    if (res.status === 409) {
      // Keep our old revision. This is important: a stale editor must not be
      // allowed to retry with the new revision and overwrite a colleague's data.
      if (body?.revision !== undefined) pendingRemoteRevision = Number(body.revision);
      scheduleConflictRefresh();
    }
    const err = new Error(body?.error || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }

  if (body?.revision !== undefined && !url.endsWith("/revision")) {
    projectRevision = Number(body.revision);
    pendingRemoteRevision = null;
    updateSyncStatus();
  }

  if (isMutation(options) && document.body.classList.contains("audit-open")) {
    setTimeout(loadAuditLog, 0);
  }

  return body.data;
}

function toast(message, error = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("error", error);
  el.classList.add("show");
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
}

function updateUserControls() {
  $("userProfileBtn").textContent = currentUser?.name || "Пользователь";
}

function openUserDialog() {
  const editing = !!currentUser;
  $("userDialogTitle").textContent = editing ? "Изменить имя" : "Как вас зовут?";
  $("userDialogHint").textContent = editing
    ? "Новое имя будет указано в следующих событиях журнала."
    : "Имя будет отображаться в журнале изменений.";
  $("userName").value = currentUser?.name || "";
  $("closeUserDialogBtn").hidden = !editing;
  if (!$("userDialog").open) $("userDialog").showModal();
  $("userName").focus();
}

async function saveUserProfile(event) {
  event.preventDefault();
  const name = $("userName").value.trim();
  if (!name) return;

  try {
    if (currentUser) {
      currentUser = await api("/api/users/me", {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({name})
      });
      toast("Имя изменено");
    } else {
      const result = await api("/api/users", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({name})
      });
      currentUser = result.user;
      userToken = result.access_token;
      localStorage.setItem(userTokenStorageKey, userToken);
      toast("Профиль создан");
    }
    updateUserControls();
    $("userDialog").close();
    if (userDialogResolve) {
      userDialogResolve();
      userDialogResolve = null;
    }
  } catch (error) {
    toast(error.message, true);
    if (error.status === 401) openUserDialog();
  }
}

async function ensureUserProfile() {
  if (userToken) {
    try {
      currentUser = await api("/api/users/me");
      updateUserControls();
      return;
    } catch (error) {
      if (error.status !== 401) throw error;
    }
  }

  await new Promise(resolve => {
    userDialogResolve = resolve;
    openUserDialog();
  });
}

function currentProject() {
  return projects.find(p => p.id === currentProjectId) || null;
}

function updateSyncStatus(message = "") {
  const el = $("syncStatus");
  if (!el) return;

  if (!currentProjectId) {
    el.textContent = "Нет проекта";
    el.className = "sync-status";
    return;
  }

  if (!currentProjectToken()) {
    el.textContent = "Требуется PIN";
    el.className = "sync-status pending";
    return;
  }

  if (message) {
    el.textContent = message;
    el.className = "sync-status pending";
    return;
  }

  if (pendingRemoteRevision !== null) {
    el.textContent = "Есть изменения коллег";
    el.className = "sync-status pending";
    return;
  }

  el.textContent = "Синхронизировано";
  el.className = "sync-status ok";
}

function editorIsBusy() {
  if (document.querySelector("dialog[open]")) return true;
  const active = document.activeElement;
  return !!active?.matches("input, textarea, select");
}

function scheduleConflictRefresh() {
  updateSyncStatus("Обновление…");
  setTimeout(async () => {
    if (!currentProjectId) return;
    if (editorIsBusy()) return;
    try {
      await refresh();
      toast("Проект был изменен коллегой. Данные обновлены.");
    } catch (_) {}
  }, 50);
}

async function loadProjects(preferredId = null) {
  projects = await api("/api/projects");
  const select = $("projectSelect");

  select.innerHTML = '<option value="">Выберите проект</option>';
  for (const project of projects) {
    const opt = document.createElement("option");
    opt.value = project.id;
    opt.textContent = project.name;
    select.appendChild(opt);
  }

  const target = preferredId || currentProjectId;
  if (target && projects.some(p => p.id === target)) {
    currentProjectId = target;
    select.value = target;
    localStorage.setItem("ipPlanManager.projectId", target);
  } else {
    currentProjectId = "";
    projectRevision = null;
    pendingRemoteRevision = null;
    select.value = "";
    localStorage.removeItem("ipPlanManager.projectId");
  }

  updateProjectControls();
}

function updateProjectControls() {
  const hasProject = !!currentProjectId;
  const hasAccess = hasProject && !!currentProjectToken();
  const project = currentProject();

  $("renameProjectBtn").disabled = !hasAccess;
  $("deleteProjectBtn").disabled = !hasAccess || !project?.can_delete;
  $("deleteProjectBtn").title = hasAccess && !project?.can_delete
    ? "Удалить проект может только его создатель"
    : "";
  $("auditBtn").disabled = !currentUser;
  $("addSiteBtn").disabled = !hasAccess;
  $("importLabel").classList.toggle("disabled", !hasAccess);

  if (!hasAccess) {
    $("addSubnetBtn").disabled = true;
    $("searchInput").disabled = true;
    $("exportBtn").classList.add("disabled");
    $("exportBtn").setAttribute("aria-disabled", "true");
    $("exportBtn").disabled = true;
  } else {
    $("exportBtn").classList.remove("disabled");
    $("exportBtn").setAttribute("aria-disabled", "false");
    $("exportBtn").disabled = false;
  }

  updateSyncStatus();
}

function openProjectDialog(mode = "create") {
  projectDialogMode = mode;
  const project = currentProject();

  if (mode === "rename" && !project) return;

  $("projectDialogTitle").textContent = mode === "rename" ? "Переименовать проект" : "Новый проект";
  $("projectDialogHint").textContent = mode === "rename"
    ? "Название изменится для всех пользователей."
    : "Каждый проект хранит собственный независимый IP-план.";
  $("projectSubmitBtn").textContent = mode === "rename" ? "Сохранить" : "Создать";
  $("projectName").value = mode === "rename" ? project.name : "";
  $("projectPIN").value = "";
  $("projectPIN").required = mode !== "rename";
  $("projectPINWrap").style.display = mode === "rename" ? "none" : "";

  $("projectDialog").showModal();
  $("projectName").focus();
}

async function saveProject(e) {
  e.preventDefault();
  const name = $("projectName").value.trim();

  try {
    if (projectDialogMode === "rename") {
      const result = await api(`/api/projects/${currentProjectId}`, {
        method: "PUT",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({name})
      });
      projectRevision = Number(result.revision ?? projectRevision);
      $("projectDialog").close();
      await loadProjects(currentProjectId);
      if (state?.project) state.project.name = result.name;
      render();
      toast("Проект переименован");
    } else {
      const created = await api("/api/projects", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({name, pin: $("projectPIN").value.trim()})
      });
      const project = created.project;
      projectTokens[project.id] = created.access_token;
      saveProjectTokens();
      $("projectDialog").close();
      await loadProjects(project.id);
      await openProject(project.id);
      toast("Проект создан");
    }
  } catch (e) {
    toast(e.message, true);
  }
}

function showUnlockDialog() {
  const project = currentProject();
  if (!project) return;
  $("unlockDialogTitle").textContent = `Открыть проект · ${project.name}`;
  $("unlockDialogDescription").textContent = project.pin_set
    ? "PIN потребуется только один раз в этом браузере."
    : "У проекта нет настроенного PIN. PIN по умолчанию: 1111";
  $("unlockPIN").value = "";
  $("unlockPIN").disabled = false;
  $("unlockSubmitBtn").disabled = false;
  if (!$("unlockDialog").open) $("unlockDialog").showModal();
  $("unlockPIN").focus();
}

async function unlockProject(e) {
  e.preventDefault();
  if (!currentProjectId) return;
  try {
    const result = await api(`/api/projects/${currentProjectId}/unlock`, {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({pin: $("unlockPIN").value.trim()})
    });
    projectTokens[currentProjectId] = result.access_token;
    saveProjectTokens();
    $("unlockDialog").close();
    await loadProjects(currentProjectId);
    await refresh();
    toast("Проект открыт. Доступ сохранен в этом браузере.");
  } catch (err) {
    toast(err.message, true);
    $("unlockPIN").select();
  }
}

async function openProject(projectId) {
  if (!projectId) {
    currentProjectId = "";
    projectRevision = null;
    pendingRemoteRevision = null;
    state = null;
    localStorage.removeItem("ipPlanManager.projectId");
    collapsed.clear();
    updateProjectControls();
    render();
    return;
  }

  currentProjectId = projectId;
  projectRevision = null;
  pendingRemoteRevision = null;
  localStorage.setItem("ipPlanManager.projectId", projectId);
  $("projectSelect").value = projectId;
  collapsed.clear();
  updateProjectControls();
  if (!currentProjectToken()) {
    state = null;
    render();
    showUnlockDialog();
    return;
  }
  try {
    await refresh();
    if (!currentProject()?.can_delete) {
      await loadProjects(currentProjectId);
    }
  } catch (err) {
    if (!currentProjectToken()) {
      state = null;
      render();
      showUnlockDialog();
      return;
    }
    throw err;
  }
}

async function deleteCurrentProject() {
  const project = currentProject();
  if (!project) return;
  if (!project.can_delete) {
    toast("Удалить проект может только его создатель", true);
    return;
  }

  if (!confirm(`Удалить проект «${project.name}» вместе со всем IP-планом?`)) return;

  try {
    await api(`/api/projects/${project.id}`, {method:"DELETE"});
    forgetProjectToken(project.id);
    state = null;
    currentProjectId = "";
    projectRevision = null;
    pendingRemoteRevision = null;
    localStorage.removeItem("ipPlanManager.projectId");
    await loadProjects();
    render();
    toast("Проект удален");
  } catch (e) {
    toast(e.message, true);
  }
}

async function pollProjectRevision() {
  if (!currentProjectId || !currentProjectToken() || document.hidden) return;

  try {
    const res = await fetch(`/api/projects/${encodeURIComponent(currentProjectId)}/revision`, {
      cache: "no-store",
      headers: {
        "X-Project-ID": currentProjectId,
        "X-Project-Token": currentProjectToken(),
        "X-User-Token": userToken
      }
    });
    const body = await res.json();
    if (res.status === 403) {
      forgetProjectToken(currentProjectId);
      state = null;
      render();
      showUnlockDialog();
      return;
    }
    if (res.status === 404) {
      const deletedName = currentProject()?.name || "Проект";
      currentProjectId = "";
      projectRevision = null;
      pendingRemoteRevision = null;
      state = null;
      localStorage.removeItem("ipPlanManager.projectId");
      await loadProjects();
      render();
      toast(`${deletedName} был удален`, true);
      return;
    }
    if (!res.ok || !body?.ok) return;

    const remoteRevision = Number(body.data?.revision ?? 0);
    if (projectRevision === null) {
      projectRevision = remoteRevision;
      updateSyncStatus();
      return;
    }

    if (remoteRevision !== projectRevision) {
      pendingRemoteRevision = remoteRevision;
      updateSyncStatus();

      if (!editorIsBusy()) {
        await refresh();
        toast("Получены изменения от коллеги");
      }
    }
  } catch (_) {
    // Temporary network failures are retried on the next poll.
  }
}

function flattenTree(nodes, site, out = [], depth = 0) {
  for (const node of nodes) {
    out.push({ type: "subnet", site, node, depth });
    for (const host of node.hosts || []) {
      out.push({ type: "host", site, node, host, depth: depth + 1 });
    }
    flattenTree(node.children || [], site, out, depth + 1);
  }
  return out;
}

function allParentOptions() {
  const list = [];
  for (const site of state?.sites || []) {
    list.push({ id: site.id, label: `${site.cidr} | ${site.name}`, depth: 0, siteName: site.name });
    const walk = (nodes, depth) => {
      for (const n of nodes) {
        list.push({
          id: n.id,
          label: `${"  ".repeat(depth)}${n.cidr} | ${n.vlan_name || n.vrf || n.description || ""}`,
          depth,
          siteName: site.name
        });
        walk(n.children || [], depth + 1);
      }
    };
    walk(site.tree || [], 1);
  }
  return list;
}

function findSubnet(id) {
  for (const site of state?.sites || []) {
    const stack = [...(site.tree || [])];
    while (stack.length) {
      const n = stack.shift();
      if (n.id === id) return { site, node: n };
      stack.unshift(...(n.children || []));
    }
  }
  return null;
}

function findHost(id) {
  for (const site of state?.sites || []) {
    const stack = [...(site.tree || [])];
    while (stack.length) {
      const n = stack.shift();
      for (const h of n.hosts || []) {
        if (h.id === id) return { site, node: n, host: h };
      }
      stack.unshift(...(n.children || []));
    }
  }
  return null;
}

function descendantStats(node) {
  let subnets = 1;
  let hosts = (node.hosts || []).length;
  for (const child of node.children || []) {
    const s = descendantStats(child);
    subnets += s.subnets;
    hosts += s.hosts;
  }
  return { subnets, hosts };
}

function closeAuditPanel() {
  $("auditPanel").classList.remove("open");
  $("auditPanel").setAttribute("aria-hidden", "true");
  document.body.classList.remove("audit-open");
}

function focusAuditTarget(anchor) {
  let target = document.getElementById(anchor);
  if (target?.matches(".row-hidden, .match-hidden")) {
    collapsed.clear();
    $("searchInput").value = "";
    render();
    target = document.getElementById(anchor);
  }
  if (!target) {
    toast("Эта строка уже удалена; показан текущий IP-план");
    target = $("project-root");
  }
  target.scrollIntoView({behavior: "smooth", block: "center"});
  target.classList.remove("audit-target-flash");
  requestAnimationFrame(() => target.classList.add("audit-target-flash"));
  setTimeout(() => target.classList.remove("audit-target-flash"), 1900);
}

function renderAuditLog(events) {
  const list = $("auditList");
  list.innerHTML = "";
  if (!events.length) {
    const empty = document.createElement("div");
    empty.className = "audit-empty";
    empty.textContent = "Изменений пока нет";
    list.appendChild(empty);
    return;
  }

  for (const event of events) {
    const anchor = /^[A-Za-z0-9_-]+$/.test(event.anchor || "")
      ? event.anchor
      : "project-root";
    const link = document.createElement("a");
    link.className = "audit-event";
    link.href = `#${anchor}`;

    const actor = document.createElement("strong");
    actor.textContent = event.user_name || "Неизвестный пользователь";
    const description = document.createElement("div");
    description.className = "audit-event-description";
    description.textContent = event.description || "Изменил(а) IP-план";
    const time = document.createElement("div");
    time.className = "audit-event-time";
    time.textContent = new Date(event.timestamp).toLocaleString("ru-RU");

    link.append(actor, description, time);
    link.addEventListener("click", clickEvent => {
      clickEvent.preventDefault();
      history.replaceState(null, "", `#${anchor}`);
      focusAuditTarget(anchor);
    });
    list.appendChild(link);
  }
}

async function loadAuditLog() {
  if (!currentProjectId || !currentProjectToken()) {
    try {
      renderAuditLog(await api("/api/system-audit"));
    } catch (error) {
      renderAuditLog([]);
    }
    return;
  }
  try {
    renderAuditLog(await api("/api/audit"));
  } catch (error) {
    const list = $("auditList");
    list.innerHTML = "";
    const message = document.createElement("div");
    message.className = "audit-empty";
    message.textContent = error.message;
    list.appendChild(message);
  }
}

async function openAuditPanel() {
  $("auditPanel").classList.add("open");
  $("auditPanel").setAttribute("aria-hidden", "false");
  document.body.classList.add("audit-open");
  $("auditList").innerHTML = '<div class="audit-empty">Загрузка…</div>';
  await loadAuditLog();
}

function render() {
  $("loading").style.display = "none";

  const nav = $("siteNav");
  const sitesEl = $("sites");
  nav.innerHTML = "";
  sitesEl.innerHTML = "";

  if (!currentProjectId) {
    $("sourceName").textContent = "Выберите или создайте проект";
    updateProjectControls();
    sitesEl.innerHTML = `
      <div class="empty-card project-start">
        <strong>Проект не открыт</strong>
        <div style="margin-top:8px">Создайте новый проект или выберите существующий в верхней панели.</div>
        <div class="empty-start-actions">
          <button class="btn primary" id="emptyNewProjectBtn">+ Создать проект</button>
        </div>
      </div>`;
    $("emptyNewProjectBtn").onclick = () => openProjectDialog("create");
    return;
  }

  if (!currentProjectToken()) {
    const project = currentProject();
    $("sourceName").textContent = project?.name || "Проект закрыт";
    updateProjectControls();
    sitesEl.innerHTML = `
      <div class="empty-card project-start">
        <strong>Требуется PIN проекта</strong>
        <div style="margin-top:8px">После первого ввода доступ сохранится в этом браузере.</div>
        <div class="empty-start-actions">
          <button class="btn primary" id="emptyUnlockProjectBtn">Ввести PIN</button>
        </div>
      </div>`;
    $("emptyUnlockProjectBtn").onclick = showUnlockDialog;
    return;
  }

  const projectName = state?.project?.name || currentProject()?.name || "Проект";
  $("sourceName").textContent = state?.source_filename
    ? `${projectName} · ${state.source_filename}`
    : `${projectName} · новый IP-план`;

  const hasSites = !!state?.sites?.length;
  $("addSubnetBtn").disabled = !hasSites;
  $("searchInput").disabled = !hasSites;
  updateProjectControls();

  if (!state?.sites?.length) {
    sitesEl.innerHTML = `
      <div class="empty-card empty-start">
        <strong>Пустой IP-план</strong>
        <div style="margin-top:8px">Можно начать с нуля, создав первую площадку, или импортировать существующий Excel.</div>
        <div class="empty-start-actions">
          <button class="btn primary" id="emptyAddSiteBtn">+ Создать площадку</button>
          <label class="btn secondary">
            Импорт Excel
            <input id="emptyFileInput" type="file" accept=".xlsx,.xlsm" hidden>
          </label>
        </div>
      </div>`;
    $("emptyAddSiteBtn").onclick = openSiteCreate;
    $("emptyFileInput").onchange = e => {
      const file = e.target.files?.[0];
      if (file) importExcel(file);
      e.target.value = "";
    };
    return;
  }

  for (const site of state.sites) {
    const navBtn = document.createElement("button");
    navBtn.className = "nav-btn";
    navBtn.innerHTML = `<strong>${esc(site.name)}</strong><span>${esc(site.cidr)}</span>`;
    navBtn.onclick = () => document.getElementById(`site-${site.id}`)?.scrollIntoView({ behavior: "smooth" });
    nav.appendChild(navBtn);

    const section = document.createElement("section");
    section.className = "site-card";
    section.id = `site-${site.id}`;
    section.innerHTML = `
      <div class="site-header">
        <div class="site-identity">
          <input class="site-inline site-name-input" data-site-field="name" data-site-id="${site.id}" value="${esc(site.name)}">
          <input class="site-inline site-cidr-input mono" data-site-field="cidr" data-site-id="${site.id}" value="${esc(site.cidr)}">
        </div>
        <div class="site-actions">
          <button class="btn secondary tiny" data-add-site="${site.id}">+ Подсеть</button>
          <button class="btn tiny danger" data-delete-site="${site.id}">Удалить площадку</button>
        </div>
      </div>
      <div class="table-wrap">
        <table class="ip-table">
          <thead><tr>
            <th>CIDR / IP</th>
            <th>Gateway</th>
            <th>VRF</th>
            <th>VLAN</th>
            <th>VLAN Name</th>
            <th>Comment</th>
            <th>Zone</th>
            <th>Site</th>
            <th>Description / Name</th>
            <th>Role</th>
            <th>Actions</th>
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>`;

    const tbody = section.querySelector("tbody");
    appendNodes(tbody, site, site.tree || [], 0, []);
    sitesEl.appendChild(section);
  }

  sitesEl.querySelectorAll("[data-add-site]").forEach(btn => {
    btn.onclick = () => openSubnetCreate(btn.dataset.addSite);
  });
  sitesEl.querySelectorAll("[data-delete-site]").forEach(btn => {
    btn.onclick = () => removeSite(btn.dataset.deleteSite);
  });
  bindSiteEditors();
  bindRowActions();
  applySearch();
}

function subnetInlinePayload(row) {
  return {
    cidr: row.querySelector('[data-subnet-field="cidr"]').value.trim(),
    gateway: row.querySelector('[data-subnet-field="gateway"]').value.trim(),
    vrf: row.querySelector('[data-subnet-field="vrf"]').value.trim(),
    vlan_number: row.querySelector('[data-subnet-field="vlan_number"]').value.trim(),
    vlan_name: row.querySelector('[data-subnet-field="vlan_name"]').value.trim(),
    comment: row.querySelector('[data-subnet-field="comment"]').value.trim(),
    zone: row.querySelector('[data-subnet-field="zone"]').value.trim(),
    site: row.querySelector('[data-subnet-field="site"]').value.trim(),
    description: row.querySelector('[data-subnet-field="description"]').value.trim()
  };
}

function hostInlinePayload(row, found) {
  const prefix = row.dataset.ipPrefix || "";
  const ipInput = row.querySelector('[data-host-field="ip"]');
  const suffix = ipInput ? ipInput.value.trim() : "";
  const fullIp = ipInput?.disabled
    ? prefix
    : (/^\d{1,3}(?:\.\d{1,3}){3}$/.test(suffix) ? suffix : prefix + suffix);

  return {
    ip: fullIp,
    name: row.querySelector('[data-host-field="name"]').value.trim(),
    subsystem: found.host.subsystem || "",
    role: row.querySelector('[data-host-field="role"]').value.trim(),
    cpu: found.host.cpu || "",
    ram: found.host.ram || "",
    disk: found.host.disk || "",
    type: found.host.type || "",
    status: found.host.status || "",
    comment: row.querySelector('[data-host-field="comment"]').value.trim()
  };
}

function markInlineSaved(input) {
  input.classList.remove("inline-error");
  input.classList.add("inline-saved");
  setTimeout(() => input.classList.remove("inline-saved"), 650);
}

function restoreInlineValue(input) {
  input.value = input.dataset.savedValue ?? "";
  input.classList.remove("inline-error");
}

async function saveSubnetInline(input) {
  const row = input.closest("tr");
  const id = row?.dataset.subnetId;
  if (!id) return;

  const oldValue = input.dataset.savedValue ?? "";
  if (input.value === oldValue) return;

  try {
    const payload = subnetInlinePayload(row);
    payload._changed_field = input.dataset.subnetField;

    const result = await api(`/api/subnets/${id}`, {
      method: "PUT",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify(payload)
    });

    if (result?.cidr !== undefined) {
      row.querySelector('[data-subnet-field="cidr"]').value = result.cidr;
    }
    if (result?.gateway !== undefined) {
      row.querySelector('[data-subnet-field="gateway"]').value = result.gateway;
    }

    const found = findSubnet(id);
    if (found) {
      Object.assign(found.node, {
        cidr: result?.cidr ?? payload.cidr,
        gateway: result?.gateway ?? payload.gateway,
        vrf: payload.vrf,
        vlan_number: payload.vlan_number === "" ? null : Number(payload.vlan_number),
        vlan_name: payload.vlan_name,
        comment: payload.comment,
        zone: payload.zone,
        site: payload.site,
        description: payload.description
      });
    }

    row.querySelectorAll("[data-subnet-field]").forEach(el => {
      el.dataset.savedValue = el.value;
    });
    markInlineSaved(input);

    if (input.dataset.subnetField === "cidr" || input.dataset.subnetField === "gateway") {
      const adjusted = result?.auto_adjusted || {};
      if (result?.hosts_adjusted > 0) {
        toast(`Подсеть обновлена, IP хостов автоматически изменены: ${result.hosts_adjusted}`);
      } else if (adjusted.gateway) {
        toast(`Gateway автоматически изменен на ${adjusted.gateway}`);
      } else if (adjusted.cidr) {
        toast(`Подсеть автоматически изменена на ${adjusted.cidr}`);
      }

      const y = window.scrollY;
      await refresh();
      requestAnimationFrame(() => window.scrollTo({top:y, behavior:"instant"}));
    }
  } catch (e) {
    input.classList.add("inline-error");
    restoreInlineValue(input);
    toast(e.message, true);
  }
}

async function saveHostInline(input) {
  const row = input.closest("tr");
  const id = row?.dataset.hostId;
  if (!id) return;

  const oldValue = input.dataset.savedValue ?? "";
  if (input.value === oldValue) return;

  const found = findHost(id);
  if (!found) return;

  try {
    const payload = hostInlinePayload(row, found);
    await api(`/api/hosts/${id}`, {
      method: "PUT",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify(payload)
    });

    Object.assign(found.host, {
      ip: payload.ip,
      name: payload.name,
      role: payload.role,
      comment: payload.comment
    });

    row.querySelectorAll("[data-host-field]").forEach(el => {
      el.dataset.savedValue = el.value;
    });
    markInlineSaved(input);

    if (input.dataset.hostField === "ip") {
      const y = window.scrollY;
      await refresh();
      requestAnimationFrame(() => window.scrollTo({top:y, behavior:"instant"}));
    }
  } catch (e) {
    input.classList.add("inline-error");
    restoreInlineValue(input);
    toast(e.message, true);
  }
}

function bindInlineEditors() {
  document.querySelectorAll("[data-subnet-field], [data-host-field]").forEach(input => {
    input.dataset.savedValue = input.value;

    input.addEventListener("keydown", e => {
      if (e.key === "Enter") {
        e.preventDefault();
        input.blur();
      } else if (e.key === "Escape") {
        e.preventDefault();
        restoreInlineValue(input);
        input.blur();
      }
    });

    input.addEventListener("blur", () => {
      if (input.hasAttribute("data-subnet-field")) {
        saveSubnetInline(input);
      } else {
        saveHostInline(input);
      }
    });
  });
}

function appendNodes(tbody, site, nodes, depth, ancestorIds) {
  for (const node of nodes) {
    const hiddenByAncestor = ancestorIds.some(id => collapsed.has(id));
    const row = document.createElement("tr");
    row.className = `subnet-row ${hiddenByAncestor ? "row-hidden" : ""}`;
    row.id = `row-${node.id}`;
    row.dataset.subnetId = node.id;
    row.dataset.search = [
      node.cidr, node.gateway, node.vrf, node.vlan_number, node.vlan_name,
      node.comment, node.zone, node.site, node.description
    ].join(" ").toLowerCase();

    const hasChildren = (node.children?.length || 0) + (node.hosts?.length || 0) > 0;
    const isCollapsed = collapsed.has(node.id);

    row.innerHTML = `
      <td>
        <div class="indent" style="padding-left:${depth * 22}px">
          ${hasChildren
            ? `<button class="tree-toggle" data-toggle="${node.id}">${isCollapsed ? "▸" : "▾"}</button>`
            : `<span class="tree-spacer"></span>`}
          <input class="inline-cell mono subnet-cidr-input" data-subnet-field="cidr" value="${esc(node.cidr)}">
        </div>
      </td>
      <td><input class="inline-cell mono" data-subnet-field="gateway" value="${esc(node.gateway)}"></td>
      <td><input class="inline-cell" data-subnet-field="vrf" value="${esc(node.vrf)}"></td>
      <td><input class="inline-cell inline-number" data-subnet-field="vlan_number" type="number" min="1" max="4094" value="${esc(node.vlan_number ?? "")}"></td>
      <td><input class="inline-cell" data-subnet-field="vlan_name" value="${esc(node.vlan_name)}"></td>
      <td><input class="inline-cell" data-subnet-field="comment" value="${esc(node.comment)}"></td>
      <td><input class="inline-cell" data-subnet-field="zone" value="${esc(node.zone)}"></td>
      <td><input class="inline-cell" data-subnet-field="site" value="${esc(node.site)}"></td>
      <td><input class="inline-cell" data-subnet-field="description" value="${esc(node.description)}"></td>
      <td></td>
      <td>
        <div class="actions">
          <button class="btn tiny" data-add-child="${node.id}">+ subnet</button>
          <button class="btn tiny" data-add-host="${node.id}">+ host</button>
          <button class="btn tiny danger" data-delete-subnet="${node.id}">Удал.</button>
        </div>
      </td>`;
    tbody.appendChild(row);

    const nextAncestors = [...ancestorIds, node.id];
    const childHidden = hiddenByAncestor || isCollapsed;

    for (const host of node.hosts || []) {
      const hr = document.createElement("tr");
      hr.className = `host-row ${childHidden ? "row-hidden" : ""}`;
      hr.id = `row-${host.id}`;
      hr.dataset.hostId = host.id;

      const prefix = fixedIpPrefix(node.cidr);
      const prefixLength = Number(String(node.cidr || "").split("/")[1]);
      const ipFullyFixed = prefixLength === 32;
      hr.dataset.ipPrefix = prefix;
      const ipSuffix = ipFullyFixed
        ? ""
        : (prefix && String(host.ip || "").startsWith(prefix)
            ? String(host.ip || "").slice(prefix.length)
            : String(host.ip || ""));

      hr.dataset.search = [
        host.ip, host.name, host.role, host.comment, host.subsystem, host.status
      ].join(" ").toLowerCase();

      hr.innerHTML = `
        <td>
          <div class="indent" style="padding-left:${(depth + 1) * 22}px">
            <span class="tree-spacer"></span>
            <div class="inline-ip">
              <span class="inline-ip-prefix">${esc(prefix)}</span>
              <input class="inline-cell mono inline-ip-suffix" data-host-field="ip" value="${esc(ipSuffix)}" ${ipFullyFixed ? "disabled" : ""}>
            </div>
          </div>
        </td>
        <td></td><td></td><td></td><td></td>
        <td><input class="inline-cell" data-host-field="comment" value="${esc(host.comment)}"></td>
        <td></td>
        <td><span class="inline-readonly">${esc(host.site)}</span></td>
        <td><input class="inline-cell" data-host-field="name" value="${esc(host.name || "")}"></td>
        <td><input class="inline-cell" data-host-field="role" value="${esc(host.role || "")}"></td>
        <td><div class="actions">
          <button
            class="host-info-btn"
            data-host-info="${host.id}"
            title="Информация о хосте"
            aria-label="Информация о хосте"
          >i</button>
          <button class="btn tiny danger" data-delete-host="${host.id}">Удал.</button>
        </div></td>`;
      tbody.appendChild(hr);
    }

    appendNodes(tbody, site, node.children || [], depth + 1, nextAncestors);
  }
}

function infoValue(value) {
  return String(value ?? "").trim();
}

function setInfoHostIp(cidr, fullIp = "") {
  const prefix = fixedIpPrefix(cidr);
  const prefixLength = Number(String(cidr || "").split("/")[1]);
  const fullyFixed = prefixLength === 32;

  $("infoHostIPPrefix").textContent = prefix;
  $("infoHostIP").dataset.prefix = prefix;
  $("infoHostIP").dataset.fullyFixed = fullyFixed ? "1" : "0";

  let editablePart = String(fullIp || "").trim();
  if (prefix && editablePart.startsWith(prefix)) {
    editablePart = editablePart.slice(prefix.length);
  }

  $("infoHostIP").value = fullyFixed ? "" : editablePart;
  $("infoHostIP").disabled = fullyFixed;
  $("infoHostIP").required = !fullyFixed;
}

function infoHostFullIp() {
  const input = $("infoHostIP");
  const prefix = input.dataset.prefix || "";
  if (input.dataset.fullyFixed === "1") return prefix;

  const entered = input.value.trim();
  if (/^\d{1,3}(?:\.\d{1,3}){3}$/.test(entered)) return entered;
  return prefix + entered;
}

function openHostInfo(hostId) {
  const found = findHost(hostId);
  if (!found) return;

  hostInfoEditId = hostId;

  const h = found.host;
  const n = found.node;

  $("hostInfoTitle").textContent = h.name
    ? `Информация о хосте · ${h.name}`
    : "Информация о хосте";
  $("hostInfoHint").textContent = h.ip
    ? `${h.ip} · ${n.cidr}`
    : n.cidr;

  setInfoHostIp(n.cidr, h.ip || "");
  $("infoHostSubnet").textContent = n.cidr || "—";
  $("infoHostSite").textContent = h.site || found.site.name || "—";
  $("infoHostSubsystem").value = infoValue(h.subsystem);
  $("infoHostName").value = infoValue(h.name);
  $("infoHostRole").value = infoValue(h.role);
  $("infoHostCPU").value = infoValue(h.cpu);
  $("infoHostRAM").value = infoValue(h.ram);
  $("infoHostDisk").value = infoValue(h.disk);
  $("infoHostType").value = infoValue(h.type);
  $("infoHostStatus").value = infoValue(h.status);
  $("infoHostComment").value = infoValue(h.comment);

  $("hostInfoDialog").showModal();
}

function hostInfoPayload() {
  return {
    ip: infoHostFullIp(),
    subsystem: $("infoHostSubsystem").value.trim(),
    name: $("infoHostName").value.trim(),
    role: $("infoHostRole").value.trim(),
    cpu: $("infoHostCPU").value.trim(),
    ram: $("infoHostRAM").value.trim(),
    disk: $("infoHostDisk").value.trim(),
    type: $("infoHostType").value.trim(),
    status: $("infoHostStatus").value.trim(),
    comment: $("infoHostComment").value.trim()
  };
}

async function saveHostInfo(e) {
  e.preventDefault();
  if (!hostInfoEditId) return;

  try {
    const payload = hostInfoPayload();
    await api(`/api/hosts/${hostInfoEditId}`, {
      method: "PUT",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify(payload)
    });

    $("hostInfoDialog").close();
    toast("Информация о хосте сохранена");
    await refresh();
  } catch (e) {
    toast(e.message, true);
  }
}

function bindRowActions() {
  document.querySelectorAll("[data-toggle]").forEach(btn => {
    btn.onclick = () => {
      const id = btn.dataset.toggle;
      collapsed.has(id) ? collapsed.delete(id) : collapsed.add(id);
      render();
    };
  });
  document.querySelectorAll("[data-add-child]").forEach(btn => btn.onclick = () => openSubnetCreate(btn.dataset.addChild));
  document.querySelectorAll("[data-delete-subnet]").forEach(btn => btn.onclick = () => removeSubnet(btn.dataset.deleteSubnet));
  document.querySelectorAll("[data-add-host]").forEach(btn => btn.onclick = () => openHostCreate(btn.dataset.addHost));
  document.querySelectorAll("[data-host-info]").forEach(btn => btn.onclick = () => openHostInfo(btn.dataset.hostInfo));
  document.querySelectorAll("[data-delete-host]").forEach(btn => btn.onclick = () => removeHost(btn.dataset.deleteHost));
  bindInlineEditors();
}

function fillParentSelect(selectedId = "") {
  const sel = $("parentSelect");
  sel.innerHTML = "";
  for (const item of allParentOptions()) {
    const opt = document.createElement("option");
    opt.value = item.id;
    opt.textContent = item.label;
    if (item.id === selectedId) opt.selected = true;
    sel.appendChild(opt);
  }
  if (!sel.value && sel.options.length) sel.selectedIndex = 0;
}

function inheritedSite(parentId) {
  for (const site of state.sites || []) {
    if (site.id === parentId) return site.name;
    const found = findSubnet(parentId);
    if (found) return found.site.name;
  }
  return "";
}

function openSiteCreate() {
  $("siteName").value = "";
  $("siteCIDR").value = "";
  $("siteDialog").showModal();
  $("siteName").focus();
}

async function createSite(e) {
  e.preventDefault();
  try {
    await api("/api/sites", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({
        name: $("siteName").value.trim(),
        cidr: $("siteCIDR").value.trim()
      })
    });
    $("siteDialog").close();
    toast("Площадка создана");
    await refresh();
  } catch (e) {
    toast(e.message, true);
  }
}

async function saveSiteInline(input) {
  const siteId = input.dataset.siteId;
  const section = input.closest(".site-card");
  if (!siteId || !section) return;

  const nameInput = section.querySelector('[data-site-field="name"]');
  const cidrInput = section.querySelector('[data-site-field="cidr"]');
  const oldValue = input.dataset.savedValue ?? "";
  if (input.value === oldValue) return;

  try {
    const result = await api(`/api/sites/${siteId}`, {
      method: "PUT",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({
        name: nameInput.value.trim(),
        cidr: cidrInput.value.trim()
      })
    });

    nameInput.value = result.name;
    cidrInput.value = result.cidr;
    nameInput.dataset.savedValue = result.name;
    cidrInput.dataset.savedValue = result.cidr;
    markInlineSaved(input);

    const y = window.scrollY;
    await refresh();
    requestAnimationFrame(() => window.scrollTo({top:y, behavior:"instant"}));
  } catch (e) {
    restoreInlineValue(input);
    toast(e.message, true);
  }
}

function bindSiteEditors() {
  document.querySelectorAll("[data-site-field]").forEach(input => {
    input.dataset.savedValue = input.value;
    input.addEventListener("keydown", e => {
      if (e.key === "Enter") {
        e.preventDefault();
        input.blur();
      } else if (e.key === "Escape") {
        e.preventDefault();
        restoreInlineValue(input);
        input.blur();
      }
    });
    input.addEventListener("blur", () => saveSiteInline(input));
  });
}

async function removeSite(siteId) {
  const site = state?.sites?.find(s => s.id === siteId);
  if (!site) return;

  let subnetCount = 0;
  let hostCount = 0;
  const walk = nodes => {
    for (const node of nodes || []) {
      subnetCount += 1;
      hostCount += (node.hosts || []).length;
      walk(node.children || []);
    }
  };
  walk(site.tree || []);

  if (!confirm(`Удалить площадку ${site.name} (${site.cidr})?\nПодсетей: ${subnetCount}, хостов: ${hostCount}.`)) return;

  try {
    await api(`/api/sites/${siteId}`, {method:"DELETE"});
    toast("Площадка удалена");
    await refresh();
  } catch (e) {
    toast(e.message, true);
  }
}

function clearSubnetForm() {
  ["subnetCIDR","subnetGateway","subnetVRF","subnetVLAN","subnetVLANName",
   "subnetZone","subnetSite","subnetComment","subnetDescription"].forEach(id => $(id).value = "");
}

function openSubnetCreate(parentId = "") {
  subnetEditId = null;
  clearSubnetForm();
  $("subnetDialogTitle").textContent = "Создать подсеть";
  $("subnetDialogHint").textContent = "Подсеть будет автоматически размещена по числовому порядку и вложенности.";
  $("parentWrap").style.display = "";
  fillParentSelect(parentId);
  $("subnetSite").value = inheritedSite($("parentSelect").value);
  $("subnetDialog").showModal();
  $("subnetCIDR").focus();
}

function openSubnetEdit(id) {
  const found = findSubnet(id);
  if (!found) return;
  subnetEditId = id;
  const n = found.node;
  $("subnetDialogTitle").textContent = "Изменить подсеть";
  $("subnetDialogHint").textContent = "Вложенные сети и хосты должны остаться внутри нового CIDR.";
  $("parentWrap").style.display = "none";
  $("subnetCIDR").value = n.cidr || "";
  $("subnetGateway").value = n.gateway || "";
  $("subnetVRF").value = n.vrf || "";
  $("subnetVLAN").value = n.vlan_number ?? "";
  $("subnetVLANName").value = n.vlan_name || "";
  $("subnetZone").value = n.zone || "";
  $("subnetSite").value = n.site || found.site.name;
  $("subnetComment").value = n.comment || "";
  $("subnetDescription").value = n.description || "";
  $("subnetDialog").showModal();
}

function subnetPayload() {
  return {
    parent_id: $("parentSelect").value,
    cidr: $("subnetCIDR").value.trim(),
    gateway: $("subnetGateway").value.trim(),
    vrf: $("subnetVRF").value.trim(),
    vlan_number: $("subnetVLAN").value.trim(),
    vlan_name: $("subnetVLANName").value.trim(),
    comment: $("subnetComment").value.trim(),
    zone: $("subnetZone").value.trim(),
    site: $("subnetSite").value.trim(),
    description: $("subnetDescription").value.trim()
  };
}

async function saveSubnet(e) {
  e.preventDefault();
  try {
    const payload = subnetPayload();
    if (subnetEditId) {
      await api(`/api/subnets/${subnetEditId}`, {
        method: "PUT", headers: {"Content-Type":"application/json"}, body: JSON.stringify(payload)
      });
      toast("Подсеть обновлена");
    } else {
      await api("/api/subnets", {
        method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(payload)
      });
      toast("Подсеть создана");
    }
    $("subnetDialog").close();
    await refresh();
  } catch (e) { toast(e.message, true); }
}

async function removeSubnet(id) {
  const found = findSubnet(id);
  if (!found) return;
  const stats = descendantStats(found.node);
  const msg = `Удалить ${found.node.cidr}?\nБудет удалено подсетей: ${stats.subnets}, хостов/интерфейсов: ${stats.hosts}.`;
  if (!confirm(msg)) return;
  try {
    await api(`/api/subnets/${id}`, {method:"DELETE"});
    toast("Подсеть удалена");
    await refresh();
  } catch (e) { toast(e.message, true); }
}

function fixedIpPrefix(cidr) {
  const [network, prefixText] = String(cidr || "").split("/");
  const prefix = Number(prefixText);
  const octets = network.split(".");
  if (octets.length !== 4 || !Number.isInteger(prefix) || prefix < 0 || prefix > 32) return "";

  const fixedOctets = Math.floor(prefix / 8);
  if (fixedOctets <= 0) return "";
  if (fixedOctets >= 4) return octets.join(".");
  return octets.slice(0, fixedOctets).join(".") + ".";
}

function setHostIpPrefix(cidr, fullIp = "") {
  hostFixedPrefix = fixedIpPrefix(cidr);

  const prefixLength = Number(String(cidr || "").split("/")[1]);
  hostAddressFullyFixed = prefixLength === 32;

  $("hostIPPrefix").textContent = hostFixedPrefix;

  let editablePart = String(fullIp || "").trim();
  if (hostFixedPrefix && editablePart.startsWith(hostFixedPrefix)) {
    editablePart = editablePart.slice(hostFixedPrefix.length);
  }

  if (hostAddressFullyFixed) {
    $("hostIP").value = "";
    $("hostIP").disabled = true;
    $("hostIP").required = false;
    $("hostIP").placeholder = "";
    return;
  }

  $("hostIP").disabled = false;
  $("hostIP").required = true;
  $("hostIP").value = editablePart;

  const fixedCount = hostFixedPrefix
    ? hostFixedPrefix.replace(/\.$/, "").split(".").length
    : 0;
  const remainingCount = Math.max(1, 4 - fixedCount);
  $("hostIP").placeholder = remainingCount === 1
    ? "2"
    : Array(remainingCount).fill("1").join(".");
}

function fullHostIpFromForm() {
  if (hostAddressFullyFixed) {
    return hostFixedPrefix;
  }

  const entered = $("hostIP").value.trim();

  // Full IPv4 can still be pasted directly.
  if (/^\d{1,3}(?:\.\d{1,3}){3}$/.test(entered)) return entered;

  return hostFixedPrefix + entered;
}

function clearHostForm() {
  hostFixedPrefix = "";
  hostAddressFullyFixed = false;
  $("hostIPPrefix").textContent = "";
  $("hostIP").disabled = false;
  $("hostIP").required = true;
  ["hostIP","hostName","hostSubsystem","hostRole","hostCPU","hostRAM","hostDisk",
   "hostType","hostStatus","hostComment"].forEach(id => $(id).value = "");
}

function openHostCreate(subnetId) {
  const found = findSubnet(subnetId);
  if (!found) return;
  hostEditId = null;
  hostSubnetId = subnetId;
  clearHostForm();
  setHostIpPrefix(found.node.cidr);
  $("hostDialogTitle").textContent = "Добавить IP хоста / интерфейса";
  $("hostDialogHint").textContent = `Подсеть ${found.node.cidr}`;
  $("hostDialog").showModal();
  $("hostIP").focus();
}

function openHostEdit(hostId) {
  const found = findHost(hostId);
  if (!found) return;
  hostEditId = hostId;
  hostSubnetId = found.node.id;
  const h = found.host;
  $("hostDialogTitle").textContent = "Изменить IP хоста / интерфейса";
  $("hostDialogHint").textContent = `Подсеть ${found.node.cidr}`;
  setHostIpPrefix(found.node.cidr, h.ip || "");
  $("hostName").value = h.name || "";
  $("hostSubsystem").value = h.subsystem || "";
  $("hostRole").value = h.role || "";
  $("hostCPU").value = h.cpu || "";
  $("hostRAM").value = h.ram || "";
  $("hostDisk").value = h.disk || "";
  $("hostType").value = h.type || "";
  $("hostStatus").value = h.status || "";
  $("hostComment").value = h.comment || "";
  $("hostDialog").showModal();
}

function hostPayload() {
  return {
    ip: fullHostIpFromForm(),
    name: $("hostName").value.trim(),
    subsystem: $("hostSubsystem").value.trim(),
    role: $("hostRole").value.trim(),
    cpu: $("hostCPU").value.trim(),
    ram: $("hostRAM").value.trim(),
    disk: $("hostDisk").value.trim(),
    type: $("hostType").value.trim(),
    status: $("hostStatus").value.trim(),
    comment: $("hostComment").value.trim()
  };
}

async function saveHost(e) {
  e.preventDefault();
  try {
    const payload = hostPayload();
    if (hostEditId) {
      await api(`/api/hosts/${hostEditId}`, {
        method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)
      });
      toast("Хост обновлен");
    } else {
      await api(`/api/subnets/${hostSubnetId}/hosts`, {
        method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)
      });
      toast("Хост добавлен");
    }
    $("hostDialog").close();
    await refresh();
  } catch (e) { toast(e.message, true); }
}

async function removeHost(id) {
  const found = findHost(id);
  if (!found || !confirm(`Удалить ${found.host.ip}?`)) return;
  try {
    await api(`/api/hosts/${id}`, {method:"DELETE"});
    toast("Хост удален");
    await refresh();
  } catch (e) { toast(e.message, true); }
}

async function importExcel(file) {
  const fd = new FormData();
  fd.append("file", file);
  try {
    const data = await api("/api/import", {method:"POST", body:fd});
    state = data;
    collapsed.clear();
    render();
    toast("Excel импортирован");
  } catch (e) { toast(e.message, true); }
}

async function exportExcel() {
  if (!currentProjectId || !currentProjectToken()) return;
  try {
    const response = await fetch("/api/export", {
      headers: {
        "X-Project-ID": currentProjectId,
        "X-Project-Token": currentProjectToken(),
        "X-User-Token": userToken
      }
    });
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try { message = (await response.json()).error || message; } catch (_) {}
      throw new Error(message);
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const utf8Name = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
    const plainName = disposition.match(/filename="?([^";]+)"?/i)?.[1];
    const filename = utf8Name ? decodeURIComponent(utf8Name) : (plainName || "IP_Plan.xlsx");
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    toast(err.message, true);
  }
}

async function refresh() {
  if (!currentProjectId) {
    state = null;
    projectRevision = null;
    pendingRemoteRevision = null;
    render();
    return;
  }

  state = await api("/api/state");
  if (state?.revision !== undefined) {
    projectRevision = Number(state.revision);
  }
  pendingRemoteRevision = null;
  updateSyncStatus();
  render();
  if (document.body.classList.contains("audit-open")) await loadAuditLog();
}

function applySearch() {
  const q = $("searchInput").value.trim().toLowerCase();
  document.querySelectorAll(".ip-table tbody tr").forEach(row => {
    row.classList.toggle("match-hidden", !!q && !row.dataset.search.includes(q));
  });
}

function esc(value) {
  return String(value ?? "")
    .replaceAll("&","&amp;").replaceAll("<","&lt;")
    .replaceAll(">","&gt;").replaceAll('"',"&quot;");
}

document.addEventListener("DOMContentLoaded", async () => {
  document.querySelectorAll("[data-close]").forEach(btn => {
    btn.onclick = () => $(btn.dataset.close).close();
  });

  $("userForm").onsubmit = saveUserProfile;
  $("userProfileBtn").onclick = openUserDialog;
  $("userDialog").addEventListener("cancel", event => {
    if (!currentUser) event.preventDefault();
  });
  $("auditBtn").onclick = openAuditPanel;
  $("closeAuditBtn").onclick = closeAuditPanel;

  $("newProjectBtn").onclick = () => openProjectDialog("create");
  $("renameProjectBtn").onclick = () => openProjectDialog("rename");
  $("deleteProjectBtn").onclick = deleteCurrentProject;
  $("projectForm").onsubmit = saveProject;
  $("unlockForm").onsubmit = unlockProject;
  $("projectSelect").onchange = async e => {
    try {
      await openProject(e.target.value);
    } catch (err) {
      toast(err.message, true);
    }
  };

  $("addSiteBtn").onclick = openSiteCreate;
  $("addSubnetBtn").onclick = () => {
    if (!state?.sites?.length) return;
    openSubnetCreate();
  };

  $("fileInput").onchange = e => {
    const file = e.target.files?.[0];
    if (!currentProjectId) {
      toast("Сначала откройте проект", true);
    } else if (file) {
      importExcel(file);
    }
    e.target.value = "";
  };
  $("exportBtn").onclick = exportExcel;

  $("searchInput").oninput = applySearch;
  $("siteForm").onsubmit = createSite;
  $("subnetForm").onsubmit = saveSubnet;
  $("hostForm").onsubmit = saveHost;
  $("hostInfoForm").onsubmit = saveHostInfo;

  $("parentSelect").onchange = () => {
    if (!subnetEditId) $("subnetSite").value = inheritedSite($("parentSelect").value);
  };

  document.addEventListener("focusout", () => {
    if (pendingRemoteRevision !== null) {
      setTimeout(() => {
        if (!editorIsBusy()) pollProjectRevision();
      }, 0);
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) pollProjectRevision();
  });

  try {
    await ensureUserProfile();
    const remembered = currentProjectId;
    await loadProjects(remembered);

    if (currentProjectId) {
      await openProject(currentProjectId);
    } else {
      render();
    }

    window.setInterval(pollProjectRevision, 1500);
  } catch (e) {
    $("loading").textContent = e.message;
    toast(e.message, true);
  }
});

