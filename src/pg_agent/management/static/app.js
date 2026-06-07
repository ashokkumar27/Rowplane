const DEFAULT_TENANT = "00000000-0000-0000-0000-000000000123";
const STATUS_ORDER = [
  "queued",
  "thinking",
  "needs_tool",
  "tool_running",
  "waiting_approval",
  "waiting_child",
  "evaluating",
  "completed",
  "failed",
  "blocked",
  "pending",
  "approved",
  "rejected",
];

const state = {
  view: "overview",
  tenantId: localStorage.getItem("pgAgentTenantId") || DEFAULT_TENANT,
  actor: localStorage.getItem("pgAgentActor") || "console_admin",
  selectedApprovalId: null,
  selectedRunId: null,
  selectedAgentId: null,
  selectedEvalCaseId: null,
};

function el(id) {
  return document.getElementById(id);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function jsonText(value) {
  if (value === undefined || value === null || value === "") {
    return "{}";
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch (_error) {
    return String(value);
  }
}

function shortId(value) {
  const text = String(value ?? "");
  if (!text) {
    return "-";
  }
  if (text.length <= 18) {
    return text;
  }
  return `${text.slice(0, 8)}...${text.slice(-4)}`;
}

function readable(value) {
  const text = String(value || "unknown").replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function formatDate(value) {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return date.toLocaleString();
}

function formatAge(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) {
    return "-";
  }
  if (value < 60) {
    return `${Math.max(0, Math.round(value))}s`;
  }
  if (value < 3600) {
    return `${Math.round(value / 60)}m`;
  }
  if (value < 86400) {
    return `${Math.round(value / 3600)}h`;
  }
  return `${Math.round(value / 86400)}d`;
}

function formatPercent(value) {
  if (value === null || value === undefined || value === "") {
    return "n/a";
  }
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "n/a";
  }
  return `${Math.round(number * 100)}%`;
}

function sumCounts(counts) {
  return Object.values(counts || {}).reduce((total, item) => total + Number(item || 0), 0);
}

function classForStatus(value) {
  return `status-${String(value || "unknown").replaceAll(" ", "_").toLowerCase()}`;
}

function statusPill(value) {
  const normalized = value === true ? "enabled" : value === false ? "disabled" : String(value || "unknown");
  return `<span class="status-pill ${classForStatus(normalized)}">${escapeHtml(readable(normalized))}</span>`;
}

function compactPayload(value, maxLength = 150) {
  const text = typeof value === "string" ? value : jsonText(value);
  return text.length > maxLength ? `${text.slice(0, maxLength)}...` : text;
}

function buildQuery(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, value);
    }
  }
  const query = search.toString();
  return query ? `?${query}` : "";
}

function showToast(message, isError = false) {
  const toast = el("toast");
  toast.textContent = message || "";
  toast.className = message ? `toast visible${isError ? " error" : ""}` : "toast";
}

function setBusy(isBusy) {
  const button = el("refreshButton");
  button.disabled = isBusy;
  button.textContent = isBusy ? "Refreshing" : "Refresh";
}

function setConnection(mode, label) {
  const chip = el("connectionState");
  chip.textContent = label;
  chip.className = mode === "error" ? "health-chip error" : mode === "ok" ? "health-chip" : "health-chip muted";
}

function updateRefreshMeta() {
  el("lastRefresh").textContent = `Refreshed ${new Date().toLocaleTimeString()}`;
  el("currentTenantLabel").textContent = `Tenant ${shortId(state.tenantId)}`;
}

function updateNavBadge(id, count) {
  const badge = el(id);
  if (!badge) {
    return;
  }
  badge.textContent = String(count || 0);
}

async function api(path, options = {}) {
  const headers = {
    "X-Tenant-ID": state.tenantId.trim(),
    "X-Actor": state.actor.trim() || "console_admin",
  };
  const fetchOptions = {
    method: options.method || "GET",
    headers,
  };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    fetchOptions.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, fetchOptions);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = payload && typeof payload === "object" ? payload.detail : payload;
    throw new Error(detail || `${response.status} ${response.statusText}`);
  }
  return payload;
}

function persistSettings() {
  state.tenantId = el("tenantId").value.trim() || DEFAULT_TENANT;
  state.actor = el("actorId").value.trim() || "console_admin";
  el("tenantId").value = state.tenantId;
  el("actorId").value = state.actor;
  el("currentTenantLabel").textContent = `Tenant ${shortId(state.tenantId)}`;
  localStorage.setItem("pgAgentTenantId", state.tenantId);
  localStorage.setItem("pgAgentActor", state.actor);
}

function emptyState(title, body = "") {
  return `<div class="empty-state"><strong>${escapeHtml(title)}</strong>${body ? `<span>${escapeHtml(body)}</span>` : ""}</div>`;
}

function loadingRows(targetId, columns) {
  el(targetId).innerHTML = `<tr><td colspan="${columns}" class="cell-muted">Loading</td></tr>`;
}

function emptyRows(targetId, columns, title, body = "") {
  el(targetId).innerHTML = `<tr><td colspan="${columns}">${emptyState(title, body)}</td></tr>`;
}

function detailEmpty(targetId, title, body = "") {
  el(targetId).innerHTML = `<div class="detail-empty">${emptyState(title, body)}</div>`;
}

function kv(label, value) {
  return `<div class="kv"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value ?? "-")}</strong></div>`;
}

function rawDetails(label, value) {
  return `
    <details class="raw-block">
      <summary>${escapeHtml(label)}</summary>
      <pre>${escapeHtml(jsonText(value))}</pre>
    </details>
  `;
}

function markSelected(type, id) {
  document.querySelectorAll(`tr[data-select="${type}"]`).forEach((row) => {
    row.classList.toggle("selected-row", row.dataset.id === id);
  });
}

function selectedClass(type, id) {
  const selected =
    (type === "approval" && state.selectedApprovalId === id) ||
    (type === "run" && state.selectedRunId === id) ||
    (type === "agent" && state.selectedAgentId === id) ||
    (type === "eval" && state.selectedEvalCaseId === id);
  return selected ? " selected-row" : "";
}

async function refreshView() {
  persistSettings();
  showToast("");
  setBusy(true);
  setConnection("muted", "Refreshing");
  try {
    if (state.view === "overview") {
      await loadOverview();
    } else if (state.view === "approvals") {
      await loadApprovals();
    } else if (state.view === "runs") {
      await loadRuns();
    } else if (state.view === "tools") {
      await loadTools();
    } else if (state.view === "agents") {
      await loadAgents();
    } else if (state.view === "evals") {
      await loadEvals();
    } else if (state.view === "audit") {
      await loadAudit();
    } else if (state.view === "memory") {
      await loadMemory();
    }
    setConnection("ok", "Connected");
    updateRefreshMeta();
  } catch (error) {
    setConnection("error", "API error");
    showToast(error.message, true);
  } finally {
    setBusy(false);
  }
}

function setView(view) {
  state.view = view;
  document.querySelectorAll(".nav-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  document.querySelectorAll(".view").forEach((section) => {
    section.classList.toggle("active", section.dataset.view === view);
  });
  refreshView();
}

async function loadOverview() {
  const overview = await api("/api/metrics/overview");
  const runCounts = overview.run_status_counts || {};
  const taskCounts = overview.task_status_counts || {};
  const queue = overview.queue_backlog || {};
  const pendingApprovals = Number(overview.pending_approvals || 0);
  const blockedRuns = Number(overview.blocked_runs || 0);
  const toolFailureRate = Number(overview.tool_failure_rate || 0);
  const totalRuns = sumCounts(runCounts);
  const totalTasks = sumCounts(taskCounts);
  const attentionTotal = pendingApprovals + blockedRuns + Number(runCounts.failed || 0);

  updateNavBadge("approvalNavBadge", pendingApprovals);
  updateNavBadge("runNavBadge", totalRuns);

  const insights = [
    {
      label: "Attention",
      value: attentionTotal ? `${attentionTotal} item${attentionTotal === 1 ? "" : "s"}` : "Clear",
      note: `${pendingApprovals} approvals, ${blockedRuns} blocked, ${runCounts.failed || 0} failed`,
      tone: attentionTotal ? "danger" : "good",
    },
    {
      label: "Queue",
      value: queue.total ? `${queue.total} waiting` : "Clear",
      note: `${queue.runs || 0} runs and ${queue.tasks || 0} tasks queued`,
      tone: queue.total ? "warn" : "good",
    },
    {
      label: "Tools",
      value: formatPercent(overview.tool_failure_rate),
      note: "failure rate across executions",
      tone: toolFailureRate > 0 ? "danger" : "good",
    },
    {
      label: "Quality",
      value: formatPercent(overview.eval_pass_rate),
      note: "latest eval result coverage",
      tone: overview.eval_pass_rate === null ? "" : Number(overview.eval_pass_rate) >= 0.9 ? "good" : "warn",
    },
  ];

  el("attentionStrip").innerHTML = insights.map((item) => `
    <article class="insight-card ${escapeHtml(item.tone || "")}">
      <div class="label">${escapeHtml(item.label)}</div>
      <strong>${escapeHtml(item.value)}</strong>
      <span>${escapeHtml(item.note)}</span>
    </article>
  `).join("");

  const metrics = [
    { label: "Runs", value: totalRuns, note: `${runCounts.completed || 0} completed`, tone: totalRuns ? "" : "warn" },
    { label: "Tasks", value: totalTasks, note: `${taskCounts.completed || 0} completed`, tone: totalTasks ? "" : "warn" },
    { label: "Pending Approvals", value: pendingApprovals, note: "approval queue", tone: pendingApprovals ? "warn" : "good" },
    { label: "Queue Backlog", value: queue.total || 0, note: `${queue.runs || 0} runs, ${queue.tasks || 0} tasks`, tone: queue.total ? "warn" : "good" },
    { label: "Tool Failure", value: formatPercent(overview.tool_failure_rate), note: "tool executions", tone: toolFailureRate > 0 ? "danger" : "good" },
    { label: "Blocked Runs", value: blockedRuns, note: "needs operator review", tone: blockedRuns ? "danger" : "good" },
  ];

  el("metricGrid").innerHTML = metrics.map((metric) => `
    <article class="metric ${escapeHtml(metric.tone || "")}">
      <div class="label">${escapeHtml(metric.label)}</div>
      <div class="value">${escapeHtml(metric.value)}</div>
      <div class="note">${escapeHtml(metric.note)}</div>
    </article>
  `).join("");

  el("runStateList").innerHTML = renderStatusCounts(runCounts, "No runs yet", "Run counts will appear after workers create runs.");
  el("taskStateList").innerHTML = renderStatusCounts(taskCounts, "No tasks yet", "Task counts appear when multi-agent work is created.");
  const events = overview.recent_events || [];
  el("recentEvents").innerHTML = events.length ? events.map(renderEventRow).join("") : emptyState("No events yet", "Agent events will appear here as workers run.");
  el("overviewUpdated").textContent = `${totalRuns} runs, ${totalTasks} tasks, ${pendingApprovals} pending approvals`;
}

function renderStatusCounts(counts, emptyTitle, emptyBody) {
  const entries = Object.entries(counts || {}).sort(([a], [b]) => {
    const aIndex = STATUS_ORDER.indexOf(a);
    const bIndex = STATUS_ORDER.indexOf(b);
    return (aIndex === -1 ? 999 : aIndex) - (bIndex === -1 ? 999 : bIndex) || a.localeCompare(b);
  });
  if (!entries.length) {
    return emptyState(emptyTitle, emptyBody);
  }
  return entries.map(([status, count]) => `
    <div class="status-row">
      ${statusPill(status)}
      <strong>${escapeHtml(count)}</strong>
    </div>
  `).join("");
}

function renderEventRow(event) {
  const type = event.event_type || event.type || "event";
  return `
    <div class="event-row">
      <div class="cell-muted">${escapeHtml(formatDate(event.created_at))}</div>
      <div>
        <strong>${escapeHtml(readable(type))}</strong>
        <div class="payload">${escapeHtml(compactPayload(event.payload || event.data || {}))}</div>
      </div>
      <div class="cell-muted">${escapeHtml(event.actor || "-")}</div>
    </div>
  `;
}

function approvalActions(approval) {
  const id = approval.approval_request_id || approval.id;
  if (approval.status !== "pending") {
    return `<span class="cell-muted">${escapeHtml(readable(approval.status))}</span>`;
  }
  return `
    <div class="actions">
      <button class="button small primary" type="button" data-action="approve" data-id="${escapeHtml(id)}">Approve</button>
      <button class="button small danger" type="button" data-action="reject" data-id="${escapeHtml(id)}">Reject</button>
    </div>
  `;
}

async function loadApprovals() {
  loadingRows("approvalRows", 5);
  const status = el("approvalStatus").value;
  const approvals = await api(`/api/approvals${buildQuery({ status })}`);
  el("approvalCount").textContent = `${approvals.length} request${approvals.length === 1 ? "" : "s"}`;
  if (status === "pending") {
    updateNavBadge("approvalNavBadge", approvals.length);
  } else if (status === "all") {
    updateNavBadge("approvalNavBadge", approvals.filter((approval) => approval.status === "pending").length);
  }
  if (!approvals.length) {
    emptyRows("approvalRows", 5, "No approvals match this filter", "Risky tool requests and human gates appear here.");
    detailEmpty("approvalDetail", "No approval selected", "Select an approval when requests are available.");
    return;
  }

  const ids = approvals.map((approval) => approval.approval_request_id || approval.id);
  if (!state.selectedApprovalId || !ids.includes(state.selectedApprovalId)) {
    state.selectedApprovalId = ids[0];
  }

  el("approvalRows").innerHTML = approvals.map((approval) => {
    const id = approval.approval_request_id || approval.id;
    return `
      <tr class="selectable${selectedClass("approval", id)}" data-select="approval" data-id="${escapeHtml(id)}">
        <td><strong>${escapeHtml(approval.tool_name || approval.payload?.tool_name || "Manual review")}</strong><div class="cell-muted">${escapeHtml(approval.reason || "No reason recorded")}</div></td>
        <td>${statusPill(approval.status)}</td>
        <td class="cell-id">${escapeHtml(shortId(approval.run_id))}</td>
        <td>${escapeHtml(formatAge(approval.age_seconds))}</td>
        <td>${approvalActions(approval)}</td>
      </tr>
    `;
  }).join("");
  await loadApprovalDetail(state.selectedApprovalId);
}

async function loadApprovalDetail(id) {
  state.selectedApprovalId = id;
  markSelected("approval", id);
  detailEmpty("approvalDetail", "Loading approval");
  const detail = await api(`/api/approvals/${encodeURIComponent(id)}`);
  const approval = detail.approval || detail;
  const execution = detail.tool_execution || {};
  const actionBlock = approval.status === "pending" ? `
    <div class="actions">
      <button class="button small primary" type="button" data-action="approve" data-id="${escapeHtml(id)}">Approve</button>
      <button class="button small danger" type="button" data-action="reject" data-id="${escapeHtml(id)}">Reject</button>
    </div>
  ` : "";

  el("approvalDetail").innerHTML = `
    <div class="detail-title">
      <div>
        <h3>${escapeHtml(approval.tool_name || approval.payload?.tool_name || "Approval request")}</h3>
        <p class="mono">${escapeHtml(id)}</p>
      </div>
      ${statusPill(approval.status)}
    </div>
    <div class="detail-body">
      ${actionBlock}
      <div class="kv-grid">
        ${kv("Run", shortId(approval.run_id))}
        ${kv("Task", shortId(approval.task_id))}
        ${kv("Run State", readable(approval.run_status || "unknown"))}
        ${kv("Task State", readable(approval.task_status || "unknown"))}
        ${kv("Requested By", approval.requested_by || "-")}
        ${kv("Age", formatAge(approval.age_seconds))}
      </div>
      <section class="section-card">
        <div class="panel-header"><h3>Request</h3></div>
        <div class="record-list">
          <div class="record-row">
            <header><strong>Reason</strong>${statusPill(approval.requires_approval ? "requires approval" : "manual")}</header>
            <div class="payload">${escapeHtml(approval.reason || "No reason recorded")}</div>
          </div>
          <div class="record-row">
            <header><strong>Payload</strong><span class="cell-muted">${escapeHtml(approval.tool_name || "-")}</span></header>
            <div class="payload">${escapeHtml(compactPayload(approval.payload || {}, 260))}</div>
          </div>
        </div>
      </section>
      <section class="section-card">
        <div class="panel-header"><h3>Recent Events</h3></div>
        <div class="event-list">${(detail.recent_events || []).map(renderEventRow).join("") || emptyState("No recent events")}</div>
      </section>
      ${rawDetails("Raw approval record", { approval, tool_execution: execution })}
    </div>
  `;
}

async function resolveApproval(id, approved) {
  let body;
  if (approved && !window.confirm("Approve this request?")) {
    return;
  }
  if (!approved) {
    const reason = window.prompt("Reject reason", "Rejected in console");
    if (reason === null) {
      return;
    }
    body = { reason };
  }
  await api(`/api/approvals/${encodeURIComponent(id)}/${approved ? "approve" : "reject"}`, {
    method: "POST",
    body,
  });
  showToast(approved ? "Approval resolved" : "Approval rejected");
  await loadApprovals();
}

async function loadRuns() {
  loadingRows("runRows", 6);
  const status = el("runStatus").value;
  const runs = await api(`/api/runs${buildQuery({ status })}`);
  el("runCount").textContent = `${runs.length} run${runs.length === 1 ? "" : "s"}`;
  if (!status) {
    updateNavBadge("runNavBadge", runs.length);
  }
  if (!runs.length) {
    emptyRows("runRows", 6, "No runs match this filter", "Run records appear after an agent run is queued.");
    detailEmpty("runDetail", "No run selected", "Select a run when records are available.");
    return;
  }

  const ids = runs.map((run) => run.run_id || run.id);
  if (!state.selectedRunId || !ids.includes(state.selectedRunId)) {
    state.selectedRunId = ids[0];
  }

  el("runRows").innerHTML = runs.map((run) => {
    const id = run.run_id || run.id;
    return `
      <tr class="selectable${selectedClass("run", id)}" data-select="run" data-id="${escapeHtml(id)}">
        <td class="cell-id">${escapeHtml(shortId(id))}</td>
        <td>${statusPill(run.status)}</td>
        <td>${escapeHtml(run.model || "-")}</td>
        <td><strong>${escapeHtml(run.task_count ?? "-")}</strong><div class="cell-muted">${escapeHtml(run.tool_execution_count ?? 0)} tools</div></td>
        <td>${escapeHtml(run.pending_approval_count ?? 0)}</td>
        <td><strong>${escapeHtml(readable(run.latest_event_type || "-"))}</strong><div class="cell-muted">${escapeHtml(formatDate(run.latest_event_at))}</div></td>
      </tr>
    `;
  }).join("");
  await loadRunDetail(state.selectedRunId);
}

async function loadRunDetail(id) {
  state.selectedRunId = id;
  markSelected("run", id);
  detailEmpty("runDetail", "Loading run");
  const [detail, timeline] = await Promise.all([
    api(`/api/runs/${encodeURIComponent(id)}`),
    api(`/api/runs/${encodeURIComponent(id)}/timeline`),
  ]);
  const run = detail.run || detail;
  const retry = ["failed", "blocked"].includes(run.status)
    ? `<button class="button small primary" type="button" data-action="retry-run" data-id="${escapeHtml(id)}">Retry run</button>`
    : "";
  el("runDetail").innerHTML = `
    <div class="detail-title">
      <div>
        <h3>Run ${escapeHtml(shortId(id))}</h3>
        <p class="mono">${escapeHtml(id)}</p>
      </div>
      ${statusPill(run.status)}
    </div>
    <div class="detail-body">
      ${retry ? `<div class="actions">${retry}</div>` : ""}
      <div class="kv-grid">
        ${kv("Model", run.model || "-")}
        ${kv("Iterations", `${run.iteration_count ?? 0}/${run.max_iterations ?? "-"}`)}
        ${kv("Tasks", (detail.tasks || []).length)}
        ${kv("Tool Calls", (detail.tool_executions || []).length)}
        ${kv("Approvals", (detail.approvals || []).length)}
        ${kv("Completed", formatDate(run.completed_at))}
      </div>
      <section class="section-card">
        <div class="panel-header"><h3>Tasks</h3></div>
        <div class="event-list">${renderTaskEvents(detail.tasks || [])}</div>
      </section>
      <section class="section-card">
        <div class="panel-header"><h3>Timeline</h3></div>
        <div class="event-list">${timeline.length ? timeline.map(renderTimelineItem).join("") : emptyState("No timeline")}</div>
      </section>
      ${rawDetails("Raw run context", { run, approvals: detail.approvals || [], tool_executions: detail.tool_executions || [] })}
    </div>
  `;
}

function renderTaskEvents(tasks) {
  if (!tasks.length) {
    return emptyState("No tasks", "Single-run workflows may not create task rows.");
  }
  return tasks.map((task) => `
    <div class="event-row">
      <div>${statusPill(task.status)}</div>
      <div>
        <strong>${escapeHtml(task.agent_name || task.agent_id || "Agent")}</strong>
        <div class="payload">${escapeHtml(compactPayload(task.output || task.error || task.input || {}, 220))}</div>
      </div>
      <div class="cell-id">${escapeHtml(shortId(task.id))}</div>
    </div>
  `).join("");
}

function renderTimelineItem(item) {
  return `
    <div class="event-row">
      <div class="cell-muted">${escapeHtml(formatDate(item.created_at))}</div>
      <div>
        <strong>${escapeHtml(readable(item.kind))}: ${escapeHtml(readable(item.type))}</strong>
        <div class="payload">${escapeHtml(compactPayload(item.data || {}, 220))}</div>
      </div>
      <div>${statusPill(item.kind)}</div>
    </div>
  `;
}

async function retryRun(id) {
  if (!window.confirm("Queue a new run from this failed or blocked run?")) {
    return;
  }
  const response = await api(`/api/runs/${encodeURIComponent(id)}/retry`, { method: "POST" });
  showToast(`Queued retry ${shortId(response.id || response.run_id)}`);
  await loadRuns();
}

async function loadTools() {
  loadingRows("toolRows", 8);
  const tools = await api("/api/tools");
  el("toolCount").textContent = `${tools.length} tool${tools.length === 1 ? "" : "s"}`;
  if (!tools.length) {
    emptyRows("toolRows", 8, "No tools registered", "Tools must be registered before workers can call them.");
    return;
  }
  el("toolRows").innerHTML = tools.map((tool) => {
    const id = tool.tool_id || tool.id;
    const name = tool.tool_name || tool.name;
    const enabled = Boolean(tool.enabled);
    const requiresApproval = Boolean(tool.requires_approval);
    const description = tool.description || "";
    return `
      <tr data-tool-id="${escapeHtml(id)}" data-original-enabled="${enabled}" data-original-approval="${requiresApproval}" data-original-description="${escapeHtml(description)}">
        <td><strong>${escapeHtml(name)}</strong><div class="cell-id">${escapeHtml(shortId(id))}</div></td>
        <td><label class="toggle-cell"><input type="checkbox" data-field="enabled" ${enabled ? "checked" : ""}><span>${escapeHtml(enabled ? "Enabled" : "Disabled")}</span></label></td>
        <td><label class="toggle-cell"><input type="checkbox" data-field="requires_approval" ${requiresApproval ? "checked" : ""}><span>${escapeHtml(requiresApproval ? "Required" : "Not required")}</span></label></td>
        <td>${statusPill(tool.is_side_effecting ? "side_effect" : "read_only")}</td>
        <td><strong>${escapeHtml(tool.execution_count ?? 0)}</strong><div class="cell-muted">${escapeHtml(tool.completed_count ?? 0)} completed</div></td>
        <td>${scoreCell(tool.failure_rate, true)}<div class="cell-muted">${escapeHtml(tool.failed_count ?? 0)} failed</div></td>
        <td><textarea class="tool-description" data-field="description">${escapeHtml(description)}</textarea></td>
        <td><button class="button small primary" type="button" data-action="save-tool" data-id="${escapeHtml(id)}">Save</button></td>
      </tr>
    `;
  }).join("");
}

async function saveTool(id) {
  const row = document.querySelector(`tr[data-tool-id="${CSS.escape(id)}"]`);
  if (!row) {
    return;
  }
  const enabled = row.querySelector('[data-field="enabled"]').checked;
  const requiresApproval = row.querySelector('[data-field="requires_approval"]').checked;
  const description = row.querySelector('[data-field="description"]').value;
  const body = {};
  if (String(enabled) !== row.dataset.originalEnabled) {
    body.enabled = enabled;
  }
  if (String(requiresApproval) !== row.dataset.originalApproval) {
    body.requires_approval = requiresApproval;
  }
  if (description !== row.dataset.originalDescription) {
    body.description = description;
  }
  if (!Object.keys(body).length) {
    showToast("No tool changes to save");
    return;
  }
  await api(`/api/tools/${encodeURIComponent(id)}`, { method: "PATCH", body });
  showToast("Tool updated");
  await loadTools();
}

async function loadAgents() {
  loadingRows("agentRows", 5);
  const agents = await api("/api/agents");
  el("agentCount").textContent = `${agents.length} agent${agents.length === 1 ? "" : "s"}`;
  if (!agents.length) {
    emptyRows("agentRows", 5, "No agents registered", "Agent roles appear after multi-agent configuration is seeded.");
    detailEmpty("agentDetail", "No agent selected", "Select an agent when records are available.");
    return;
  }

  const ids = agents.map((agent) => agent.id);
  if (!state.selectedAgentId || !ids.includes(state.selectedAgentId)) {
    state.selectedAgentId = ids[0];
  }

  el("agentRows").innerHTML = agents.map((agent) => `
    <tr class="selectable${selectedClass("agent", agent.id)}" data-select="agent" data-id="${escapeHtml(agent.id)}">
      <td><strong>${escapeHtml(agent.name)}</strong><div class="cell-id">${escapeHtml(shortId(agent.id))}</div></td>
      <td>${escapeHtml(agent.role || "-")}</td>
      <td>${statusPill(Boolean(agent.enabled))}</td>
      <td><strong>${escapeHtml(agent.task_count ?? 0)}</strong><div class="cell-muted">${escapeHtml(agent.failed_task_count ?? 0)} failed</div></td>
      <td>${escapeHtml(agent.blocked_task_count ?? 0)}</td>
    </tr>
  `).join("");
  await loadAgentDetail(state.selectedAgentId);
}

async function loadAgentDetail(id) {
  state.selectedAgentId = id;
  markSelected("agent", id);
  detailEmpty("agentDetail", "Loading agent");
  const detail = await api(`/api/agents/${encodeURIComponent(id)}`);
  const agent = detail.agent || detail;
  const permissions = detail.tool_permissions || [];
  el("agentDetail").innerHTML = `
    <div class="detail-title">
      <div>
        <h3>${escapeHtml(agent.name || "Agent")}</h3>
        <p class="mono">${escapeHtml(id)}</p>
      </div>
      ${statusPill(Boolean(agent.enabled))}
    </div>
    <div class="detail-body">
      <div class="kv-grid">
        ${kv("Role", agent.role || "-")}
        ${kv("Model", agent.model || "-")}
        ${kv("Tool Grants", permissions.filter((item) => item.allowed).length)}
        ${kv("Failed Tasks", detail.task_status_counts?.failed || 0)}
      </div>
      <section class="section-card">
        <div class="panel-header"><h3>Tool Permissions</h3></div>
        <div class="record-list">${permissions.length ? permissions.map(renderPermission).join("") : emptyState("No tool permissions")}</div>
      </section>
      ${rawDetails("Raw agent record", detail)}
    </div>
  `;
}

function renderPermission(permission) {
  return `
    <div class="record-row">
      <header><strong>${escapeHtml(permission.tool_name || permission.tool_id || "Tool")}</strong>${statusPill(Boolean(permission.allowed))}</header>
      <div class="payload">${escapeHtml(permission.subject_type || "agent")} ${escapeHtml(shortId(permission.subject_id))}</div>
    </div>
  `;
}

async function loadEvals() {
  loadingRows("evalRows", 6);
  const evals = await api("/api/evals");
  el("evalCount").textContent = `${evals.length} eval case${evals.length === 1 ? "" : "s"}`;
  if (!evals.length) {
    emptyRows("evalRows", 6, "No eval cases", "Eval summaries appear after eval cases are created.");
    detailEmpty("evalDetail", "No eval selected", "Select an eval case when records are available.");
    return;
  }

  const ids = evals.map((item) => item.eval_case_id || item.id);
  if (!state.selectedEvalCaseId || !ids.includes(state.selectedEvalCaseId)) {
    state.selectedEvalCaseId = ids[0];
  }

  el("evalRows").innerHTML = evals.map((item) => {
    const id = item.eval_case_id || item.id;
    return `
      <tr class="selectable${selectedClass("eval", id)}" data-select="eval" data-id="${escapeHtml(id)}">
        <td><strong>${escapeHtml(item.eval_case_name || item.name || id)}</strong><div class="cell-id">${escapeHtml(shortId(id))}</div></td>
        <td>${escapeHtml(item.result_count ?? 0)}</td>
        <td>${scoreCell(item.avg_correctness ?? item.correctness)}</td>
        <td>${scoreCell(item.avg_tool_correctness ?? item.tool_correctness)}</td>
        <td>${scoreCell(item.avg_policy_compliance ?? item.policy_compliance)}</td>
        <td>${escapeHtml(formatDate(item.latest_result_at || item.created_at))}</td>
      </tr>
    `;
  }).join("");
  await loadEvalDetail(state.selectedEvalCaseId);
}

function scoreCell(value, invertTone = false) {
  if (value === null || value === undefined || value === "") {
    return `<span class="cell-muted">n/a</span>`;
  }
  const number = Math.max(0, Math.min(1, Number(value)));
  if (!Number.isFinite(number)) {
    return `<span class="cell-muted">n/a</span>`;
  }
  const percent = Math.round(number * 100);
  const color = invertTone
    ? percent > 0 ? "var(--red)" : "var(--green)"
    : percent >= 90 ? "var(--green)" : percent >= 70 ? "var(--amber)" : "var(--red)";
  return `
    <div class="score-bar">
      <strong>${escapeHtml(`${percent}%`)}</strong>
      <div class="score-track"><div class="score-fill" style="width: ${percent}%; background: ${color}"></div></div>
    </div>
  `;
}

async function loadEvalDetail(id) {
  state.selectedEvalCaseId = id;
  markSelected("eval", id);
  detailEmpty("evalDetail", "Loading eval results");
  const results = await api(`/api/evals/${encodeURIComponent(id)}/results`);
  el("evalDetail").innerHTML = `
    <div class="detail-title">
      <div>
        <h3>Eval Results</h3>
        <p class="mono">${escapeHtml(id)}</p>
      </div>
      <span class="status-pill">${escapeHtml(results.length)} rows</span>
    </div>
    <div class="detail-body">
      <section class="section-card">
        <div class="panel-header"><h3>Recent Results</h3></div>
        <div class="event-list">${results.length ? results.map(renderEvalResult).join("") : emptyState("No results")}</div>
      </section>
    </div>
  `;
}

function renderEvalResult(result) {
  const scores = result.scores || {
    correctness: result.correctness,
    tool_correctness: result.tool_correctness,
    retrieval_relevance: result.retrieval_relevance,
    format_compliance: result.format_compliance,
    policy_compliance: result.policy_compliance,
  };
  return `
    <div class="event-row">
      <div class="cell-muted">${escapeHtml(formatDate(result.created_at))}</div>
      <div>
        <strong>${escapeHtml(result.eval_case_name || "Result")}</strong>
        <div class="payload">correctness ${escapeHtml(formatPercent(scores.correctness))}, tools ${escapeHtml(formatPercent(scores.tool_correctness))}, policy ${escapeHtml(formatPercent(scores.policy_compliance))}</div>
      </div>
      <div class="cell-id">${escapeHtml(shortId(result.run_id))}</div>
    </div>
  `;
}

async function loadAudit() {
  loadingRows("auditRows", 6);
  const rows = await api(`/api/audit/events${buildQuery({ event_type: el("auditType").value.trim(), actor: el("auditActor").value.trim() })}`);
  el("auditCount").textContent = `${rows.length} event${rows.length === 1 ? "" : "s"}`;
  if (!rows.length) {
    emptyRows("auditRows", 6, "No audit events match this filter", "Management and run events appear after activity is recorded.");
    return;
  }
  el("auditRows").innerHTML = rows.map((row) => `
    <tr>
      <td>${escapeHtml(formatDate(row.created_at))}</td>
      <td>${statusPill(row.source || "agent_event")}</td>
      <td><strong>${escapeHtml(readable(row.event_type || "-"))}</strong></td>
      <td>${escapeHtml(row.actor || "-")}</td>
      <td class="cell-id">${escapeHtml(shortId(row.run_id))}</td>
      <td class="payload">${escapeHtml(compactPayload(row.payload || {}, 220))}</td>
    </tr>
  `).join("");
}

async function loadMemory() {
  const rows = await api(`/api/memory${buildQuery({ memory_type: el("memoryType").value.trim() })}`);
  el("memoryCount").textContent = `${rows.length} memor${rows.length === 1 ? "y" : "ies"}`;
  if (!rows.length) {
    el("memoryList").innerHTML = `<div class="panel">${emptyState("No memories match this filter", "Typed memory appears after a run records learning.")}</div>`;
    return;
  }
  el("memoryList").innerHTML = rows.map((memory) => `
    <article class="memory-card">
      <header>
        <h3>${escapeHtml(readable(memory.memory_type || "memory"))}</h3>
        <span class="cell-muted">${escapeHtml(formatDate(memory.created_at))}</span>
      </header>
      <p>${escapeHtml(memory.content || "")}</p>
      <div class="cell-id">Source ${escapeHtml(shortId(memory.source_run_id))}</div>
      ${rawDetails("Metadata", memory.metadata || {})}
    </article>
  `).join("");
}

function handleDocumentClick(event) {
  const action = event.target.closest("[data-action]");
  if (action) {
    const id = action.dataset.id;
    if (action.dataset.action === "approve") {
      resolveApproval(id, true).catch((error) => showToast(error.message, true));
    } else if (action.dataset.action === "reject") {
      resolveApproval(id, false).catch((error) => showToast(error.message, true));
    } else if (action.dataset.action === "retry-run") {
      retryRun(id).catch((error) => showToast(error.message, true));
    } else if (action.dataset.action === "save-tool") {
      saveTool(id).catch((error) => showToast(error.message, true));
    }
    event.stopPropagation();
    return;
  }

  const row = event.target.closest("tr[data-select]");
  if (!row || event.target.closest("button,input,textarea,select")) {
    return;
  }
  const id = row.dataset.id;
  if (row.dataset.select === "approval") {
    loadApprovalDetail(id).catch((error) => showToast(error.message, true));
  } else if (row.dataset.select === "run") {
    loadRunDetail(id).catch((error) => showToast(error.message, true));
  } else if (row.dataset.select === "agent") {
    loadAgentDetail(id).catch((error) => showToast(error.message, true));
  } else if (row.dataset.select === "eval") {
    loadEvalDetail(id).catch((error) => showToast(error.message, true));
  }
}

document.addEventListener("DOMContentLoaded", () => {
  el("tenantId").value = state.tenantId;
  el("actorId").value = state.actor;
  el("currentTenantLabel").textContent = `Tenant ${shortId(state.tenantId)}`;

  document.querySelectorAll(".nav-button").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
  });

  el("refreshButton").addEventListener("click", refreshView);
  el("approvalStatus").addEventListener("change", refreshView);
  el("runStatus").addEventListener("change", refreshView);
  el("auditType").addEventListener("change", refreshView);
  el("auditActor").addEventListener("change", refreshView);
  el("memoryType").addEventListener("change", refreshView);
  el("tenantId").addEventListener("change", refreshView);
  el("actorId").addEventListener("change", persistSettings);
  document.addEventListener("click", handleDocumentClick);

  refreshView();
});
