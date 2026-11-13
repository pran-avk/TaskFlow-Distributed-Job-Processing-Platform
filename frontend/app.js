"use strict";

// TaskFlow dashboard. Plain JavaScript, no build step; served by the API at /ui/.
// All user-provided data is rendered with textContent, never innerHTML.

const STORAGE_KEY = "taskflow.session";
const JOBS_REFRESH_MS = 2000;
const SYSTEM_REFRESH_MS = 5000;
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
