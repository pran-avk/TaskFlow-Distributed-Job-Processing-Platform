"use strict";

// TaskFlow dashboard. Plain JavaScript, no build step; served by the API at /ui/.
// All user-provided data is rendered with textContent, never innerHTML.

const STORAGE_KEY = "taskflow.session";
const JOBS_REFRESH_MS = 2000;
const SYSTEM_REFRESH_MS = 2000;
const PIPELINE_WINDOW_MS = 5 * 60 * 1000;
const SVG_NS = "http://www.w3.org/2000/svg";
// In-progress stages, in flow order (also the stacking order, bottom to top).
const STAGES = [
  { key: "PENDING", label: "Pending" },
  { key: "QUEUED", label: "Queued" },
  { key: "RUNNING", label: "Running" },
  { key: "RETRY_WAIT", label: "Retry wait" },
];
const STAGE_LABEL = Object.fromEntries(STAGES.map((s) => [s.key, s.label]));
const OUTCOMES = [
  { key: "SUCCEEDED", label: "Succeeded" },
  { key: "FAILED", label: "Failed" },
  { key: "DEAD_LETTER", label: "Dead letter" },
  { key: "CANCELLED", label: "Cancelled" },
];
const TERMINAL = new Set(["SUCCEEDED", "FAILED", "DEAD_LETTER", "CANCELLED"]);
const RETRYABLE = new Set(["FAILED", "DEAD_LETTER", "CANCELLED"]);

const $ = (selector) => document.querySelector(selector);

const state = {
  session: null,          // { email, access, refresh }
  jobTypes: [],           // [{ job_type, payload_schema, timeout_seconds, description }]
  payloadMode: "form",
  pageCursors: [null],    // cursor for each visited page
  pageIndex: 0,
  nextCursor: null,
  openJobId: null,
  timers: [],
  samples: [],            // [{ t, PENDING, QUEUED, RUNNING, RETRY_WAIT }] for the pipeline chart
  lastStats: null,
  showPipelineTable: false,
};

// ---------------------------------------------------------------- helpers

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "className") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined) node.append(child);
  }
  return node;
}

function toast(message, isError = false) {
  const node = el("div", { className: isError ? "toast error" : "toast", text: message });
  $("#toasts").append(node);
  setTimeout(() => node.remove(), isError ? 6000 : 3000);
}

function badge(status) {
  return el("span", { className: `badge ${status}`, text: status.replace("_", " ") });
}

function shortId(id) {
  return id.slice(0, 8);
}

function relativeTime(iso) {
  if (!iso) return "—";
  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  const future = seconds < 0;
  const abs = Math.abs(seconds);
  let text;
  if (abs < 5) return future ? "in a moment" : "just now";
  if (abs < 60) text = `${abs}s`;
  else if (abs < 3600) text = `${Math.floor(abs / 60)}m`;
  else if (abs < 86400) text = `${Math.floor(abs / 3600)}h`;
  else text = `${Math.floor(abs / 86400)}d`;
  return future ? `in ${text}` : `${text} ago`;
}

function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== undefined && value !== null) node.setAttribute(key, value);
  }
  for (const child of children.flat()) if (child) node.append(child);
  return node;
}

function formatDuration(ms) {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  const minutes = Math.floor(ms / 60000);
  return `${minutes}m ${Math.round((ms % 60000) / 1000)}s`;
}

function compactNumber(n) {
  if (n < 10000) return n.toLocaleString();
  return `${(n / 1000).toFixed(n < 100000 ? 1 : 0).replace(/\.0$/, "")}K`;
}

function niceMax(value) {
  if (value <= 4) return 4;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 2, 2.5, 5, 10]) {
    if (step * magnitude >= value) return step * magnitude;
  }
  return 10 * magnitude;
}

// ---------------------------------------------------------------- tooltip

function showTooltip(event, title, rows, total) {
  const tip = $("#chart-tooltip");
  tip.replaceChildren(
    el("div", { className: "tt-title", text: title }),
    ...rows.map((row) =>
      el("div", { className: `tt-row ${row.className || ""}` },
        row.className ? el("span", { className: "swatch" }) : null,
        el("span", { text: row.label }),
        el("span", { className: "num", text: row.value }))),
    total ? el("div", { className: "tt-row tt-total" }, el("span", { text: total.label }), el("span", { className: "num", text: total.value })) : null
  );
  tip.hidden = false;
  const rect = tip.getBoundingClientRect();
  let left = event.clientX + 14;
  let top = event.clientY + 14;
  if (left + rect.width > window.innerWidth - 8) left = event.clientX - rect.width - 14;
  if (top + rect.height > window.innerHeight - 8) top = event.clientY - rect.height - 14;
  tip.style.left = `${Math.max(8, left)}px`;
  tip.style.top = `${Math.max(8, top)}px`;
}

function hideTooltip() {
  $("#chart-tooltip").hidden = true;
}

function formatTime(iso) {
  return iso ? new Date(iso).toLocaleString() : "—";
}

function pretty(value) {
  return value === null || value === undefined ? "—" : JSON.stringify(value, null, 2);
}

function errorMessage(body, status) {
  const detail = body && body.detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => `${(d.loc || []).filter((p) => p !== "body").join(".")}: ${d.msg}`).join("; ");
  }
  return detail || `Request failed (${status})`;
}

// ---------------------------------------------------------------- session

function loadSession() {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function saveSession(session) {
  state.session = session;
  try {
    if (session) sessionStorage.setItem(STORAGE_KEY, JSON.stringify(session));
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    /* storage unavailable: session lasts until reload */
  }
}

// ---------------------------------------------------------------- API

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function refreshAccessToken() {
  if (!state.session || !state.session.refresh) return false;
  const response = await fetch("/auth/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: state.session.refresh }),
  });
  if (!response.ok) return false;
  const body = await response.json();
  saveSession({ ...state.session, access: body.access_token });
  return true;
}

async function api(path, { method = "GET", body, headers = {}, retry = true } = {}) {
  const init = { method, headers: { ...headers } };
  if (state.session) init.headers.Authorization = `Bearer ${state.session.access}`;
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  if (response.status === 401 && retry && state.session) {
    if (await refreshAccessToken()) return api(path, { method, body, headers, retry: false });
    signOut("Your session expired. Please sign in again.");
    throw new ApiError("Session expired", 401);
  }
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!response.ok) throw new ApiError(errorMessage(data, response.status), response.status);
  return data;
}

// ---------------------------------------------------------------- auth view

function showAuth() {
  stopTimers();
  $("#app-view").hidden = true;
  $("#auth-view").hidden = false;
}

function switchAuthTab(tab) {
  document.querySelectorAll("[data-auth-tab]").forEach((button) => {
    button.classList.toggle("active", button.dataset.authTab === tab);
  });
  $("#login-form").hidden = tab !== "login";
  $("#signup-form").hidden = tab !== "signup";
}

async function onLogin(event) {
  event.preventDefault();
  const form = new FormData(event.target);
  const credentials = { email: form.get("email"), password: form.get("password") };
  try {
    const body = await api("/auth/login", { method: "POST", body: credentials, retry: false });
    saveSession({ email: credentials.email, access: body.access_token, refresh: body.refresh_token });
    event.target.reset();
    startApp();
  } catch (error) {
    toast(error.message, true);
  }
}

async function onSignup(event) {
  event.preventDefault();
  const form = new FormData(event.target);
  const payload = Object.fromEntries(form.entries());
  try {
    const body = await api("/auth/signup", { method: "POST", body: payload, retry: false });
    // The signup endpoint names the access token "ascess_token".
    saveSession({ email: payload.email, access: body.ascess_token || body.access_token, refresh: body.refresh_token });
    event.target.reset();
    toast("Account created");
    startApp();
  } catch (error) {
    toast(error.message, true);
  }
}

function signOut(message) {
  saveSession(null);
  state.samples = [];
  hideTooltip();
  closeJob();
  showAuth();
  if (message) toast(message, true);
}

// ---------------------------------------------------------------- job form

function describe(schema) {
  return schema.description || null;
}

function humanize(name) {
  const words = name.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function fieldInput(name, schema, required) {
  const title = humanize(name);
  const help = describe(schema);
  const labelText = required ? title : `${title} (optional)`;

  // List of choices -> checkboxes
  if (schema.type === "array" && schema.items && Array.isArray(schema.items.enum)) {
    const defaults = new Set(schema.default || []);
    const boxes = schema.items.enum.map((option) =>
      el("label", { className: "check" },
        el("input", { type: "checkbox", value: option, checked: defaults.has(option) || undefined }),
        option)
    );
    return el("div", { className: "field", dataset: { name, kind: "multi" } },
      el("div", { className: "small", text: labelText }),
      el("div", { className: "checks" }, boxes));
  }

  let input;
  if (Array.isArray(schema.enum)) {
    input = el("select", { name },
      schema.enum.map((option) => el("option", { value: option, text: option, selected: option === schema.default || undefined })));
  } else if (schema.type === "boolean") {
    return el("label", { className: "check field", dataset: { name, kind: "boolean" } },
      el("input", { type: "checkbox", name, checked: schema.default === true || undefined }),
      title);
  } else if (schema.type === "integer" || schema.type === "number") {
    input = el("input", {
      type: "number",
      name,
      step: schema.type === "integer" ? "1" : "any",
      min: schema.minimum ?? schema.exclusiveMinimum,
      max: schema.maximum ?? schema.exclusiveMaximum,
      value: schema.default,
      required: required && schema.default === undefined,
    });
  } else if ((schema.maxLength || 0) > 300) {
    input = el("textarea", { name, rows: 5, maxlength: schema.maxLength, required: required || undefined });
    if (schema.default !== undefined) input.value = schema.default;
  } else {
    input = el("input", {
      name,
      maxlength: schema.maxLength,
      minlength: schema.minLength,
      value: schema.default,
      required: required && schema.default === undefined,
    });
  }
  const kind = schema.type === "integer" ? "integer" : schema.type === "number" ? "number" : "string";
  return el("label", { className: "field", dataset: { name, kind } },
    labelText,
    input,
    help ? el("span", { className: "field-help", text: help }) : null);
}

function currentJobType() {
  return state.jobTypes.find((t) => t.job_type === $("#job-type").value);
}

function renderPayloadFields() {
  const type = currentJobType();
  const container = $("#payload-fields");
  container.replaceChildren();
  if (!type) return;
  const schema = type.payload_schema;
  const required = new Set(schema.required || []);
  for (const [name, prop] of Object.entries(schema.properties || {})) {
    container.append(fieldInput(name, prop, required.has(name)));
  }
  if (!container.children.length) {
    container.append(el("p", { className: "muted small", text: "This job type takes no input." }));
  }
  $("#job-type-help").textContent =
    [type.description || type.payload_schema.description, `Timeout ${type.timeout_seconds}s`].filter(Boolean).join(" · ");
  $("#payload-json").value = JSON.stringify(collectFormPayload(), null, 2);
}

function collectFormPayload() {
  const payload = {};
  for (const field of $("#payload-fields").querySelectorAll(".field")) {
    const { name, kind } = field.dataset;
    if (kind === "multi") {
      const chosen = [...field.querySelectorAll("input:checked")].map((box) => box.value);
      if (chosen.length) payload[name] = chosen; // none ticked: let the server apply its default
      continue;
    }
    const input = field.querySelector("input, select, textarea");
    if (kind === "boolean") {
      payload[name] = input.checked;
      continue;
    }
    if (input.value === "") continue; // let the server apply defaults
    payload[name] = kind === "integer" ? parseInt(input.value, 10)
      : kind === "number" ? parseFloat(input.value)
      : input.value;
  }
  return payload;
}

function setPayloadMode(mode) {
  if (mode === "json" && state.payloadMode === "form") {
    $("#payload-json").value = JSON.stringify(collectFormPayload(), null, 2);
  }
  state.payloadMode = mode;
  document.querySelectorAll("[data-mode]").forEach((chip) => chip.classList.toggle("active", chip.dataset.mode === mode));
  $("#payload-fields").hidden = mode !== "form";
  $("#payload-json-wrap").hidden = mode !== "json";
}

async function onSubmitJob(event) {
  event.preventDefault();
  const form = new FormData(event.target);
  let payload;
  if (state.payloadMode === "json") {
    try {
      payload = JSON.parse($("#payload-json").value || "{}");
    } catch {
      toast("Payload is not valid JSON", true);
      return;
    }
  } else {
    payload = collectFormPayload();
  }

  const body = {
    job_type: form.get("job_type"),
    payload,
    priority: parseInt(form.get("priority"), 10),
    max_attempts: parseInt(form.get("max_attempts"), 10),
  };
  const scheduled = form.get("scheduled_at");
  if (scheduled) body.scheduled_at = new Date(scheduled).toISOString();
  const headers = {};
  const key = (form.get("idempotency_key") || "").trim();
  if (key) headers["Idempotency-Key"] = key;

  const button = event.target.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const job = await api("/api/v1/jobs", { method: "POST", body, headers });
    toast(`Submitted ${body.job_type} · ${shortId(job.job_id)}`);
    goToFirstPage();
    await loadJobs();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function loadJobTypes() {
  state.jobTypes = await api("/api/v1/jobs/types/schemas");
  const select = $("#job-type");
  const filter = $("#filter-type");
  select.replaceChildren(...state.jobTypes.map((t) => el("option", { value: t.job_type, text: t.job_type })));
  filter.replaceChildren(
    el("option", { value: "", text: "All types" }),
    ...state.jobTypes.map((t) => el("option", { value: t.job_type, text: t.job_type }))
  );
  renderPayloadFields();
}

// ---------------------------------------------------------------- job list

function goToFirstPage() {
  state.pageCursors = [null];
  state.pageIndex = 0;
}

async function loadJobs() {
  const params = new URLSearchParams({ limit: "25" });
  const status = $("#filter-status").value;
  const type = $("#filter-type").value;
  const cursor = state.pageCursors[state.pageIndex];
  if (status) params.set("status", status);
  if (type) params.set("job_type", type);
  if (cursor) params.set("cursor", cursor);

  const page = await api(`/api/v1/jobs?${params}`);
  state.nextCursor = page.next_cursor;
  renderJobs(page.items);
}

function renderJobs(items) {
  const rows = items.map((job) =>
    el("tr", { onclick: () => openJob(job.job_id), title: job.job_id },
      el("td", { className: "mono", text: shortId(job.job_id) }),
      el("td", { text: job.job_type }),
      el("td", {}, badge(job.status)),
      el("td", { text: String(job.priority) }),
      el("td", { text: `${job.attempt_count} / ${job.max_attempts}` }),
      el("td", { className: "muted", title: formatTime(job.created_at), text: relativeTime(job.created_at) }))
  );
  $("#jobs-body").replaceChildren(...rows);
  $("#jobs-empty").hidden = items.length > 0;
  $("#prev-page").disabled = state.pageIndex === 0;
  $("#next-page").disabled = !state.nextCursor;
  $("#page-label").textContent = `Page ${state.pageIndex + 1}`;
}

// ---------------------------------------------------------------- job details

async function openJob(jobId) {
  state.openJobId = jobId;
  // Clear the previous job's details before the new ones arrive.
  $("#d-title").textContent = "Loading…";
  $("#d-meta").replaceChildren();
  for (const id of ["#d-result", "#d-error", "#d-payload"]) $(id).textContent = "";
  $("#d-attempts").replaceChildren();
  $("#d-timeline").replaceChildren();
  $("#d-timeline-legend").replaceChildren();
  $("#d-timeline-table").replaceChildren();
  $("#d-error-wrap").hidden = true;
  const dialog = $("#job-dialog");
  if (!dialog.open) dialog.showModal();
  try {
    await renderJob();
  } catch (error) {
    toast(error.message, true);
  }
}

function closeJob() {
  state.openJobId = null;
  const dialog = $("#job-dialog");
  if (dialog.open) dialog.close();
}

async function renderJob() {
  const jobId = state.openJobId;
  if (!jobId) return;
  const [job, attempts] = await Promise.all([
    api(`/api/v1/jobs/${jobId}`),
    api(`/api/v1/jobs/${jobId}/attempts`),
  ]);
  if (state.openJobId !== jobId) return;

  $("#d-title").textContent = job.job_type;
  $("#d-status").replaceWith(Object.assign(badge(job.status), { id: "d-status" }));

  const meta = [
    ["Job ID", job.job_id],
    ["Priority", String(job.priority)],
    ["Attempts", `${job.attempt_count} / ${job.max_attempts}`],
    ["Created", formatTime(job.created_at)],
    ["Scheduled", formatTime(job.scheduled_at)],
    ["Started", formatTime(job.started_at)],
    ["Completed", formatTime(job.completed_at)],
  ];
  if (job.idempotency_key) meta.push(["Idempotency key", job.idempotency_key]);
  if (job.cancel_requested && !TERMINAL.has(job.status)) meta.push(["Cancellation", "requested"]);
  $("#d-meta").replaceChildren(...meta.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]));

  $("#d-result").textContent = job.status === "SUCCEEDED" ? pretty(job.result) : `No result yet (${job.status})`;
  $("#d-error-wrap").hidden = !job.last_error;
  $("#d-error").textContent = pretty(job.last_error);
  $("#d-payload").textContent = pretty(job.payload);

  $("#d-cancel").disabled = TERMINAL.has(job.status) || (job.status === "RUNNING" && job.cancel_requested);
  $("#d-retry").disabled = !RETRYABLE.has(job.status);

  const rows = attempts.map((a) =>
    el("tr", {},
      el("td", { text: String(a.attempt_number) }),
      el("td", {}, badge(a.status)),
      el("td", { className: "mono", text: a.worker_id }),
      el("td", { text: a.duration_ms === null ? "—" : `${a.duration_ms} ms` }),
      el("td", { text: a.error_type ? `${a.error_type}: ${a.error_message || ""}` : "—" }))
  );
  if (!rows.length) rows.push(el("tr", {}, el("td", { colspan: "5", className: "muted", text: "Not started yet" })));
  $("#d-attempts").replaceChildren(...rows);
  renderTimeline(job, attempts);
}

async function jobAction(action) {
  const jobId = state.openJobId;
  if (!jobId) return;
  try {
    await api(`/api/v1/jobs/${jobId}/${action}`, { method: "POST" });
    toast(action === "cancel" ? "Cancellation requested" : "Job queued for retry");
    await Promise.all([renderJob(), loadJobs()]);
  } catch (error) {
    toast(error.message, true);
  }
}

// ---------------------------------------------------------------- system panel

async function loadSystem() {
  const [stats, workers] = await Promise.all([api("/api/v1/queues/stats"), api("/api/v1/workers")]);
  recordSample(stats);
  const stat = (value, label) =>
    el("div", { className: "stat" }, el("div", { className: "value", text: String(value) }), el("div", { className: "label", text: label }));

  const healthy = workers.filter((w) => w.healthy).length;
  const workerList = workers.length
    ? workers.map((w) =>
        el("li", { title: w.worker_id },
          el("span", { className: w.healthy ? "dot ok" : "dot" }),
          el("span", { className: "name mono", text: w.worker_id }),
          el("span", { className: "muted", text: w.healthy ? `${w.active_jobs}/${w.concurrency} busy` : "offline" })))
    : [el("li", { className: "muted", text: "No workers running" })];

  $("#system").replaceChildren(
    el("div", { className: "stats" },
      stat(stats.queues.high, "High queue"),
      stat(stats.queues.normal, "Normal queue"),
      stat(stats.queues.low, "Low queue"),
      stat(stats.in_flight_messages, "In flight"),
      stat(stats.outbox_pending, "Awaiting dispatch"),
      stat(`${healthy}/${workers.length}`, "Healthy workers")),
    el("ul", { className: "workers" }, workerList)
  );
}

// ---------------------------------------------------------------- pipeline chart

function recordSample(stats) {
  const counts = stats.jobs_by_status || {};
  const sample = { t: Date.now() };
  for (const stage of STAGES) sample[stage.key] = counts[stage.key] || 0;
  state.samples.push(sample);
  const cutoff = sample.t - PIPELINE_WINDOW_MS;
  while (state.samples.length && state.samples[0].t < cutoff) state.samples.shift();
  state.lastStats = stats;
  renderPipeline();
}

function renderPipeline() {
  const latest = state.samples[state.samples.length - 1];
  const counts = (state.lastStats && state.lastStats.jobs_by_status) || {};

  $("#stage-legend").replaceChildren(...STAGES.map((stage) =>
    el("li", { className: `stage-${stage.key}` },
      el("span", { className: "swatch" }),
      stage.label,
      el("span", { className: "value", text: latest ? compactNumber(latest[stage.key]) : "—" }))));

  $("#outcomes").replaceChildren(...OUTCOMES.map((outcome) =>
    el("div", { className: `outcome ${outcome.key}` },
      el("div", { className: "value", text: compactNumber(counts[outcome.key] || 0) }),
      el("div", { className: "label", text: outcome.label }))));

  renderPipelineChart();
  if (state.showPipelineTable) renderPipelineTable();
}

function renderPipelineChart() {
  const box = $("#pipeline-chart");
  const width = box.clientWidth;
  const height = box.clientHeight;
  if (!width || !height) return;

  const pad = { left: 44, right: 10, top: 10, bottom: 22 };
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;
  const now = Date.now();
  const start = now - PIPELINE_WINDOW_MS;
  const samples = state.samples;
  const totals = samples.map((s) => STAGES.reduce((sum, stage) => sum + s[stage.key], 0));
  const yMax = niceMax(Math.max(1, ...totals));
  const x = (t) => pad.left + ((t - start) / PIPELINE_WINDOW_MS) * plotWidth;
  const y = (v) => pad.top + plotHeight - (v / yMax) * plotHeight;

  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });

  for (let i = 1; i <= 4; i++) {
    const value = (yMax * i) / 4;
    const yy = Math.round(y(value)) + 0.5;
    root.append(svg("line", { class: "grid", x1: pad.left, x2: width - pad.right, y1: yy, y2: yy }));
    root.append(svg("text", { class: "tick", x: pad.left - 8, y: yy + 4, "text-anchor": "end" }, compactNumber(value)));
  }
  const base = Math.round(y(0)) + 0.5;
  root.append(svg("line", { class: "baseline", x1: pad.left, x2: width - pad.right, y1: base, y2: base }));
  root.append(svg("text", { class: "tick", x: pad.left - 8, y: base + 4, "text-anchor": "end" }, "0"));
  for (let minutes = 5; minutes >= 0; minutes--) {
    const label = minutes === 0 ? "now" : `−${minutes}m`;
    const anchor = minutes === 5 ? "start" : minutes === 0 ? "end" : "middle";
    root.append(svg("text", { class: "tick", x: x(now - minutes * 60000), y: height - 4, "text-anchor": anchor }, label));
  }

  if (samples.length < 2) {
    root.append(svg("text", { class: "empty", x: pad.left + plotWidth / 2, y: pad.top + plotHeight / 2, "text-anchor": "middle" }, "Collecting data…"));
    box.replaceChildren(root);
    return;
  }

  // Stacked areas: a 12% wash per stage with a 2px edge along its top.
  let lower = samples.map(() => 0);
  const tops = {};
  const edges = [];
  for (const stage of STAGES) {
    const upper = samples.map((s, i) => lower[i] + s[stage.key]);
    const topPoints = samples.map((s, i) => `${x(s.t).toFixed(1)},${y(upper[i]).toFixed(1)}`);
    const bottomPoints = samples.map((s, i) => `${x(s.t).toFixed(1)},${y(lower[i]).toFixed(1)}`).reverse();
    root.append(svg("path", { class: `area stage-${stage.key}`, d: `M${topPoints.join("L")}L${bottomPoints.join("L")}Z` }));
    edges.push(svg("path", { class: `edge stage-${stage.key}`, d: `M${topPoints.join("L")}` }));
    tops[stage.key] = upper;
    lower = upper;
  }
  // Paint edges top stage first, so when a thin stage sits on a thick one the thick
  // stage's line stays visible instead of being hidden under the thin one's.
  root.append(...edges.reverse());

  // Hover layer: crosshair, ringed dots on each stage's top edge, tooltip.
  const crosshair = svg("line", { class: "crosshair", y1: pad.top, y2: pad.top + plotHeight, visibility: "hidden" });
  const dots = STAGES.map((stage) => svg("circle", { class: `dot stage-${stage.key}`, r: 4, visibility: "hidden" }));
  const overlay = svg("rect", { x: pad.left, y: pad.top, width: plotWidth, height: plotHeight, fill: "transparent" });
  overlay.addEventListener("pointermove", (event) => {
    const bounds = root.getBoundingClientRect();
    const px = ((event.clientX - bounds.left) / bounds.width) * width;
    let nearest = 0;
    for (let i = 1; i < samples.length; i++) {
      if (Math.abs(x(samples[i].t) - px) < Math.abs(x(samples[nearest].t) - px)) nearest = i;
    }
    const sample = samples[nearest];
    const sx = x(sample.t);
    crosshair.setAttribute("x1", sx);
    crosshair.setAttribute("x2", sx);
    crosshair.setAttribute("visibility", "visible");
    STAGES.forEach((stage, i) => {
      dots[i].setAttribute("cx", sx);
      dots[i].setAttribute("cy", y(tops[stage.key][nearest]));
      dots[i].setAttribute("visibility", sample[stage.key] > 0 ? "visible" : "hidden");
    });
    showTooltip(
      event,
      new Date(sample.t).toLocaleTimeString(),
      STAGES.map((stage) => ({ className: `stage-${stage.key}`, label: stage.label, value: sample[stage.key].toLocaleString() })),
      { label: "In progress", value: totals[nearest].toLocaleString() }
    );
  });
  overlay.addEventListener("pointerleave", () => {
    crosshair.setAttribute("visibility", "hidden");
    dots.forEach((dot) => dot.setAttribute("visibility", "hidden"));
    hideTooltip();
  });
  root.append(crosshair, ...dots, overlay);
  box.replaceChildren(root);
}

function renderPipelineTable() {
  const rows = state.samples.slice(-10).reverse().map((sample) =>
    el("tr", {},
      el("td", { text: new Date(sample.t).toLocaleTimeString() }),
      ...STAGES.map((stage) => el("td", { text: sample[stage.key].toLocaleString() }))));
  $("#pipeline-table").replaceChildren(...rows);
}

function togglePipelineTable() {
  state.showPipelineTable = !state.showPipelineTable;
  $("#pipeline-table-wrap").hidden = !state.showPipelineTable;
  const button = $("#pipeline-table-toggle");
  button.textContent = state.showPipelineTable ? "Hide table" : "Show table";
  button.setAttribute("aria-pressed", String(state.showPipelineTable));
  if (state.showPipelineTable) renderPipelineTable();
}

// ---------------------------------------------------------------- job timeline

function jobSegments(job, attempts) {
  const ms = (iso) => (iso ? new Date(iso).getTime() : null);
  const now = Date.now();
  const terminal = TERMINAL.has(job.status);
  const end = terminal ? (ms(job.completed_at) ?? now) : now;
  const segments = [];
  const push = (stage, from, to, attempt) => {
    if (from !== null && to !== null && to > from) segments.push({ stage, from, to, attempt });
  };

  let cursor = ms(job.created_at);
  const ordered = [...attempts].sort((a, b) => a.attempt_number - b.attempt_number);
  ordered.forEach((attempt, i) => {
    const waiting = i === 0 ? "PENDING" : "RETRY_WAIT";
    const queued = ms(attempt.queued_at);
    const started = ms(attempt.started_at);
    if (queued !== null && queued >= cursor && queued <= started) {
      push(waiting, cursor, queued, attempt.attempt_number);
      push("QUEUED", queued, started, attempt.attempt_number);
    } else {
      push(waiting, cursor, started, attempt.attempt_number);
    }
    const finished = ms(attempt.finished_at) ?? (i === ordered.length - 1 ? end : started);
    push("RUNNING", started, finished, attempt.attempt_number);
    cursor = finished;
  });

  if (!terminal && job.status !== "RUNNING") {
    const waiting = ordered.length ? "RETRY_WAIT" : "PENDING";
    const queued = ms(job.queued_at);
    if (job.status === "QUEUED" && queued !== null && queued >= cursor) {
      push(waiting, cursor, queued);
      push("QUEUED", queued, now);
    } else {
      push(job.status === "QUEUED" ? "QUEUED" : waiting, cursor, now);
    }
  } else if (terminal && !ordered.length) {
    push(job.queued_at ? "QUEUED" : "PENDING", cursor, end); // cancelled before it ran
  }
  return segments;
}

const measureCanvas = document.createElement("canvas").getContext("2d");
function textWidth(text) {
  measureCanvas.font = "600 11px system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";
  return measureCanvas.measureText(text).width;
}

function renderTimeline(job, attempts) {
  const segments = jobSegments(job, attempts);
  const total = segments.reduce((sum, s) => sum + (s.to - s.from), 0);
  const box = $("#d-timeline");
  if (!segments.length || total <= 0) {
    box.replaceChildren(el("p", { className: "timeline-empty", text: "No timing data yet." }));
    $("#d-timeline-legend").replaceChildren();
    $("#d-timeline-table").replaceChildren();
    return;
  }

  const describeSegment = (s) => STAGE_LABEL[s.stage] + (s.attempt && s.stage !== "PENDING" ? ` · attempt ${s.attempt}` : "");
  const blocks = segments.map((s) => {
    const duration = s.to - s.from;
    const share = `${Math.round((duration / total) * 100)}%`;
    const node = el("div", {
      className: `seg stage-${s.stage}`,
      tabindex: "0",
      role: "img",
      "aria-label": `${describeSegment(s)}: ${formatDuration(duration)}, ${share} of the job's time`,
    });
    node.style.flex = `${duration} 1 0`;
    const tip = (event) => showTooltip(event, describeSegment(s), [
      { label: "Duration", value: formatDuration(duration) },
      { label: "Share", value: share },
      { label: "From", value: new Date(s.from).toLocaleTimeString() },
    ]);
    node.addEventListener("pointermove", tip);
    node.addEventListener("pointerleave", hideTooltip);
    node.addEventListener("focus", () => {
      const r = node.getBoundingClientRect();
      tip({ clientX: r.left + r.width / 2, clientY: r.bottom });
    });
    node.addEventListener("blur", hideTooltip);
    return { node, label: formatDuration(duration) };
  });
  box.replaceChildren(...blocks.map((b) => b.node));
  // Print a duration inside a block only when it fits with padding on both sides.
  requestAnimationFrame(() => {
    for (const block of blocks) {
      if (block.node.clientWidth >= textWidth(block.label) + 14) block.node.textContent = block.label;
    }
  });

  const perStage = new Map();
  for (const s of segments) perStage.set(s.stage, (perStage.get(s.stage) || 0) + (s.to - s.from));
  $("#d-timeline-legend").replaceChildren(...[...perStage].map(([stage, duration]) =>
    el("li", { className: `stage-${stage}` },
      el("span", { className: "swatch" }),
      STAGE_LABEL[stage],
      el("span", { className: "value", text: formatDuration(duration) }))));

  $("#d-timeline-table").replaceChildren(...segments.map((s) =>
    el("tr", {},
      el("td", { text: STAGE_LABEL[s.stage] }),
      el("td", { text: s.attempt ? String(s.attempt) : "—" }),
      el("td", { text: new Date(s.from).toLocaleTimeString() }),
      el("td", { text: formatDuration(s.to - s.from) }),
      el("td", { text: `${Math.round(((s.to - s.from) / total) * 100)}%` }))));
}

// ---------------------------------------------------------------- lifecycle

function stopTimers() {
  state.timers.forEach(clearInterval);
  state.timers = [];
}

function every(ms, fn) {
  const tick = () => {
    if (document.visibilityState === "visible" && state.session) fn().catch(() => {});
  };
  state.timers.push(setInterval(tick, ms));
}

async function startApp() {
  $("#auth-view").hidden = true;
  $("#app-view").hidden = false;
  $("#user-email").textContent = state.session.email || "";
  try {
    await loadJobTypes();
    await Promise.all([loadJobs(), loadSystem()]);
  } catch (error) {
    if (error.status !== 401) toast(error.message, true);
    return;
  }
  stopTimers();
  every(JOBS_REFRESH_MS, async () => {
    await loadJobs();
    if (state.openJobId) await renderJob();
  });
  every(SYSTEM_REFRESH_MS, loadSystem);
}

function bindEvents() {
  document.querySelectorAll("[data-auth-tab]").forEach((button) =>
    button.addEventListener("click", () => switchAuthTab(button.dataset.authTab)));
  $("#login-form").addEventListener("submit", onLogin);
  $("#signup-form").addEventListener("submit", onSignup);
  $("#logout").addEventListener("click", () => signOut());

  $("#job-type").addEventListener("change", renderPayloadFields);
  document.querySelectorAll("[data-mode]").forEach((chip) =>
    chip.addEventListener("click", () => setPayloadMode(chip.dataset.mode)));
  $("#job-form").addEventListener("submit", onSubmitJob);

  const refilter = () => { goToFirstPage(); loadJobs().catch((e) => toast(e.message, true)); };
  $("#filter-status").addEventListener("change", refilter);
  $("#filter-type").addEventListener("change", refilter);
  $("#prev-page").addEventListener("click", () => {
    state.pageIndex = Math.max(0, state.pageIndex - 1);
    loadJobs().catch((e) => toast(e.message, true));
  });
  $("#next-page").addEventListener("click", () => {
    if (!state.nextCursor) return;
    state.pageCursors = state.pageCursors.slice(0, state.pageIndex + 1).concat(state.nextCursor);
    state.pageIndex += 1;
    loadJobs().catch((e) => toast(e.message, true));
  });

  $("#pipeline-table-toggle").addEventListener("click", togglePipelineTable);
  let resizeFrame = 0;
  window.addEventListener("resize", () => {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(renderPipelineChart);
  });

  $("#d-close").addEventListener("click", closeJob);
  // Esc closes the dialog. Use "cancel" (synchronous) rather than "close", which fires
  // later and could clear a job opened in the meantime.
  $("#job-dialog").addEventListener("cancel", () => { state.openJobId = null; });
  $("#d-cancel").addEventListener("click", () => jobAction("cancel"));
  $("#d-retry").addEventListener("click", () => jobAction("retry"));
  $("#d-copy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(state.openJobId);
      toast("Job ID copied");
    } catch {
      toast(state.openJobId);
    }
  });
}

bindEvents();
state.session = loadSession();
if (state.session) startApp();
else showAuth();
