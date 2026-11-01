/* AgentPilot run inspector.
 *
 * One scrolling report: what the run bought, how the evidence was tiered, where
 * the search spent its attempts, and — for any single attempt — why the policy
 * chose it.  Every number here is read from the run's own records; anything the
 * runtime does not persist is shown as "not recorded" rather than inferred.
 */

const SVG = "http://www.w3.org/2000/svg";

const state = {
  runId: null,
  data: null,
  index: null,
  selected: null,
  drawerTab: "summary",
  diffCache: new Map(),
  attemptFilter: "recent",
  refreshing: false,
  researchOpen: new Set(),
};

/* ---------- tiny DOM helpers ---------- */

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  apply(node, props, children);
  return node;
}

function svg(tag, props = {}, children = []) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined) continue;
    if (key === "text") node.textContent = String(value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) if (child) node.appendChild(child);
  return node;
}

function apply(node, props, children) {
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined) continue;
    if (key === "text") node.textContent = String(value);
    else if (key === "html") node.innerHTML = value;
    else if (key === "class") node.className = value;
    else if (key === "style") Object.assign(node.style, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) if (child) node.appendChild(child);
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
}

function fmt(value, digits = 3) {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";
}

function pct(value) {
  return typeof value === "number" && Number.isFinite(value)
    ? `${(value * 100).toFixed(1)}%`
    : "—";
}

function points(value, digits = 1) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  if (isRateMetric()) {
    const scaled = value * 100;
    return `${scaled >= 0 ? "+" : ""}${scaled.toFixed(digits)} pp`;
  }
  return `${value >= 0 ? "+" : ""}${value.toFixed(Math.max(digits, 2))}`;
}

function attemptNumber(action) {
  if (typeof action?.display_attempt_number === "number") return action.display_attempt_number;
  if (typeof action?.attempt_number === "number") return action.attempt_number;
  return typeof action?.ordinal === "number" ? action.ordinal + 1 : "—";
}

function allAttempts(data = state.data) {
  return [...(data?.prehistory?.actions || []), ...(data?.actions || [])];
}

function metricLabel(value) {
  return humanize(value || "primary metric");
}

function isRateMetric() {
  return /(accuracy|success|pass|rate|fraction|proportion)/i.test(
    state.data?.meta?.metric_name || "",
  );
}

function metricValue(value) {
  return isRateMetric() ? pct(value) : fmt(value, 3);
}

/* An id like "ucb-action-3d2fa92c-…" only needs its distinguishing tail. */
function tail(value) {
  const text = String(value ?? "");
  if (!text || text === "root") return "root";
  const parts = text.split("-");
  return parts.length > 1 ? parts[parts.length - 1].slice(0, 6) : text.slice(0, 8);
}

/* Machine verdicts like "constraints_pass_and_statistic_above_margin" read as
 * sentences once the tokens are unpacked. */
function humanize(code) {
  return String(code ?? "")
    .split(":")
    .map((part) => part.replace(/_/g, " ").trim())
    .filter(Boolean)
    .join(" — ");
}

function policyLabel(method, full = false) {
  const labels = full
    ? { ucb: "UCB-based Policy Learning", memory: "Textual Policy Learning" }
    : { ucb: "UCB", memory: "Textual" };
  return labels[method] || humanize(method);
}

function readable(text) {
  return String(text ?? "").replace(/^\s*\[[A-Za-z]+\d+\]\s*/, "");
}

function directionHeadline(text) {
  const full = readable(text).trim();
  if (full.length <= 140) return full;
  const firstClause = full.split(/[,;]/, 1)[0].trim();
  return firstClause.length >= 30 ? firstClause : full;
}

function directionScope(direction) {
  return [
    ...(direction.declared_scope || []),
    ...(direction.new_paths || []).map((item) => ({ ...item, kind: "new path" })),
  ];
}

function scopeLabel(scope) {
  const detail = scope.symbols?.length ? scope.symbols.join(", ") : scope.role;
  return `${scope.path}${detail ? ` — ${detail}` : ""}`;
}

function cellLabel(cellId) {
  const brief = state.index?.cellBriefById?.get(String(cellId ?? ""));
  if (brief?.title) return brief.title;
  const text = String(cellId ?? "");
  return humanize(text.replace(/-/g, " ")) || "Unnamed cell";
}

function lifecycleLabel(action) {
  const labels = {
    planned: "Planned",
    running: "Implementing",
    evaluating: "Measuring",
    blocked: "Blocked",
    aborted: "Stopped",
    completed: "Completed",
    built: "Built · unmeasured",
  };
  return labels[action.lifecycle_stage] || humanize(action.lifecycle_stage || "planned");
}

function lifecycleClass(action) {
  if (["blocked", "aborted"].includes(action.lifecycle_stage)) return "rejected";
  if (["running", "evaluating"].includes(action.lifecycle_stage)) return "active-stage";
  if (action.lifecycle_stage === "completed") return "success";
  return "other";
}

function archiveVerdict(action) {
  if (action.prehistory?.warm_start_target) {
    return { label: "Warm-start accepted", className: "admitted" };
  }
  if (action.evaluation.admitted === true) return { label: "Added", className: "admitted" };
  if (action.evaluation.admitted === false) return { label: "Not added", className: "rejected" };
  return { label: "Pending", className: "other" };
}

function displayAdmitted(action) {
  return action.prehistory?.warm_start_target || action.evaluation.admitted === true;
}

function memoryMechanisms(content) {
  return parseMemoryRules(content).map((rule) => rule.failure);
}

function parseMemoryRules(content) {
  const rules = [];
  let current = null;
  for (const rawLine of String(content || "").split("\n")) {
    const line = rawLine.trim();
    if (/^##\s+\d+\./.test(line)) {
      if (current) rules.push(current);
      current = { applies: "", failure: "", response: "", target: "", evidence: "" };
      continue;
    }
    if (!current || !line.startsWith("- ") || !line.includes(":")) continue;
    const [rawKey, ...rest] = line.slice(2).split(":");
    const value = rest.join(":").replace(/\\/g, "").trim();
    const key = rawKey.trim().toLowerCase();
    if (key === "applies when") current.applies = value;
    else if (key === "failure mechanism") current.failure = readable(value);
    else if (key === "implementation guidance") current.response = readable(value);
    else if (key === "targets") current.target = value;
    else if (key === "evidence basis") current.evidence = value;
  }
  if (current) rules.push(current);
  return rules.filter((rule) => rule.failure || rule.response || rule.target);
}

/* Indices within a slate can differ in the third decimal, so dividing by the
 * maximum makes every bar look full.  Stretch the observed range instead, and
 * report an exact tie as a tie rather than inventing a difference. */
function scaler(values) {
  const finite = values.filter((value) => typeof value === "number");
  if (!finite.length) return { of: () => 0, tied: false };
  const min = Math.min(...finite);
  const max = Math.max(...finite);
  if (max - min < 1e-9) return { of: () => 0, tied: true };
  return {
    of: (value) =>
      typeof value === "number" ? 0.1 + 0.9 * ((value - min) / (max - min)) : 0,
    tied: false,
  };
}

function passRate(cells) {
  const values = Object.values(cells || {}).filter((item) => item !== null);
  if (!values.length) return null;
  return values.filter(Boolean).length / values.length;
}

/* ---------- shared chart furniture ---------- */

function getCss(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/* A parent is identified by its commit, so hashing the sha gives every parent a
 * stable colour across panels without a palette that runs out. */
function shaColor(sha) {
  let hue = 0;
  for (const ch of String(sha ?? "")) hue = (hue * 31 + ch.charCodeAt(0)) % 360;
  return `hsl(${hue}, 52%, 44%)`;
}

/* Horizontal gridlines with their value labels down the left gutter. */
function gridY(canvas, x0, x1, ticks, format) {
  for (const tick of ticks) {
    canvas.appendChild(
      svg("line", { x1: x0, x2: x1, y1: tick.y, y2: tick.y, stroke: "#efeee9" }),
    );
    canvas.appendChild(
      svg("text", {
        x: x0 - 7,
        y: tick.y + 3.5,
        text: format(tick.v),
        fill: "#a3a7b0",
        "font-size": 10,
        "text-anchor": "end",
      }),
    );
  }
}

/* Attempt numbers along the bottom, thinned out once they would collide. */
function axisX(canvas, count, x, yBase) {
  const every = count > 60 ? 10 : count > 34 ? 5 : count > 18 ? 2 : 1;
  const indices = [];
  for (let i = 0; i < count; i += 1) {
    if (i % every === 0 || i === count - 1) indices.push(i);
  }
  if (indices.length > 1 && indices[indices.length - 1] - indices[indices.length - 2] < every) {
    indices.splice(indices.length - 2, 1);
  }
  for (const i of indices) {
    canvas.appendChild(
      svg("text", {
        x: x(i),
        y: yBase,
        text: i + 1,
        fill: "#a3a7b0",
        "font-size": 10,
        "text-anchor": "middle",
      }),
    );
  }
}

/* Alternating background bands, one per planning batch: every batch re-runs the
 * policy, so attempts inside a band were chosen under the same posterior. */
function batchBands(canvas, x, yTop, height, batches = state.data.batches) {
  let cursor = 0;
  batches.forEach((batch, bi) => {
    const count = batch.planned_action_ids.length;
    if (!count) return;
    const x1 = x(cursor) - 8;
    const x2 = x(cursor + count - 1) + 8;
    if (bi % 2 === 1) {
      canvas.insertBefore(
        svg("rect", {
          x: x1,
          y: yTop,
          width: Math.max(x2 - x1, 6),
          height,
          fill: "#f3f2ed",
          class: "batch-band",
        }),
        canvas.firstChild,
      );
    }
    cursor += count;
  });
}

function isBetter(value, incumbent) {
  if (incumbent === null || incumbent === undefined) return true;
  return state.data.meta.metric_direction === "minimize"
    ? value < incumbent
    : value > incumbent;
}

/* Keep the archive's multi-parent history readable: an admitted archive record starts
 * the following green regime half an attempt after its node.  The blue record
 * line remains the best challenger, so it may also turn on a rejected point. */
function generationOverlay(
  canvas,
  x,
  yTop,
  yBottom,
  xRight,
  { labels = true, actions = state.data.actions } = {},
) {
  // Concurrent research branches do not create a sequential generation boundary.
  if (state.data.research) return;
  const root = state.data.programs.find((program) => program.archive_seq === 0);
  let incumbent = root?.primary_value ?? null;
  const records = [];
  actions.forEach((action, ordinal) => {
    const value = action.evaluation.child_primary_value;
    if (
      displayAdmitted(action) &&
      typeof value === "number" &&
      isBetter(value, incumbent)
    ) {
      records.push({ action, ordinal });
      incumbent = value;
    }
  });
  if (!records.length || actions.length < 2) return;
  const half = (x(1) - x(0)) / 2;
  records.forEach((record, index) => {
    const { action, ordinal } = record;
    const start = x(ordinal) + half;
    const end = index + 1 < records.length
      ? x(records[index + 1].ordinal) + half
      : xRight;
    if (end > start) {
      const greenBand = svg("rect", {
          x: start,
          y: yTop,
          width: end - start,
          height: yBottom - yTop,
          fill: getCss("--green"),
          opacity: Math.min(0.03 + 0.025 * index, 0.08),
          "pointer-events": "none",
        });
      const batchBands = canvas.querySelectorAll(".batch-band");
      const afterBatchBands = batchBands.length
        ? batchBands[batchBands.length - 1].nextSibling
        : canvas.firstChild;
      canvas.insertBefore(greenBand, afterBatchBands);
    }
    canvas.appendChild(
      svg("line", {
        x1: start,
        x2: start,
        y1: yTop,
        y2: yBottom,
        stroke: getCss("--green"),
        opacity: 0.45,
        "stroke-width": 1,
        "stroke-dasharray": "3 3",
        "pointer-events": "none",
      }),
    );
    if (!labels || end - start < 18) return;
    const wide = end - start > 130;
    const label = wide
      ? `gen ${index + 2} — from #${attemptNumber(action)}`
      : end - start >= 42
        ? `gen ${index + 2}`
        : `g${index + 2}`;
    const overflow = start + 5 + label.length * 5.6 > xRight;
    canvas.appendChild(
      svg("text", {
        x: overflow ? xRight - 3 : start + 5,
        y: yTop + 11 + (index % 2) * 12,
        text: label,
        "text-anchor": overflow ? "end" : "start",
        fill: getCss("--green"),
        opacity: 0.8,
        "font-size": 9.5,
        "pointer-events": "none",
      }),
    );
  });
}

/* ---------- tooltip ---------- */

const tooltip = document.getElementById("tooltip");

function showTip(event, title, lines) {
  tooltip.innerHTML =
    `<div class="t-title">${esc(title)}</div>` +
    lines.filter(Boolean).map((line) => `<div>${line}</div>`).join("");
  tooltip.style.display = "block";
  const box = tooltip.getBoundingClientRect();
  tooltip.style.left = `${Math.min(event.clientX + 14, window.innerWidth - box.width - 10)}px`;
  tooltip.style.top = `${Math.min(event.clientY + 14, window.innerHeight - box.height - 10)}px`;
}

function hideTip() {
  tooltip.style.display = "none";
}

/* ---------- data ---------- */

function buildIndex(data) {
  const attempts = allAttempts(data);
  attempts.forEach((item, index) => { item.display_attempt_number = index + 1; });
  const actionById = new Map(attempts.map((item) => [item.action_id, item]));
  const decisions = [...(data.prehistory?.decisions || []), ...(data.decisions || [])];
  const decisionByAction = new Map(decisions.map((item) => [item.action_id, item]));
  const programByAction = new Map();
  const programByParentId = new Map();
  for (const program of data.programs) {
    programByParentId.set(program.parent_id, program);
    if (program.source_action_id) programByAction.set(program.source_action_id, program);
  }
  const failuresByBatch = new Map();
  for (const failure of data.planning_failures || []) {
    failuresByBatch.set(
      failure.batch_id,
      (failuresByBatch.get(failure.batch_id) || 0) + 1,
    );
  }
  const actionByCommit = new Map();
  for (const action of attempts) {
    const sha = action.evaluation.child_commit_sha || action.execution.result_commit_sha;
    if (sha) actionByCommit.set(sha, action);
  }
  const cellBriefById = new Map(
    (data.meta.visible_a_evidence || []).map((brief) => [brief.cell_id, brief]),
  );
  return {
    actionById,
    actionByCommit,
    decisionByAction,
    programByAction,
    programByParentId,
    failuresByBatch,
    cellBriefById,
  };
}

async function loadRuns() {
  const response = await fetch("/api/runs");
  const { runs } = await response.json();
  const select = document.getElementById("run-select");
  clear(select);
  for (const runId of runs) select.appendChild(el("option", { value: runId, text: runId }));
  // A provenance-aware inspector may serve source runs beside the active run.
  // Open the lexically latest run by default so adding those immutable sources
  // does not make the browser appear to have gone backwards to an old run.
  if (runs.length) await loadRun(runs[runs.length - 1]);
}

async function loadRun(runId) {
  const response = await fetch(`/api/run/${encodeURIComponent(runId)}`);
  state.runId = runId;
  state.data = await response.json();
  state.index = buildIndex(state.data);
  state.selected = null;
  state.drawerTab = "summary";
  state.diffCache = new Map();
  state.attemptFilter = "recent";
  state.researchOpen = new Set();
  document.getElementById("run-select").value = runId;
  render();
}

async function refreshRun() {
  if (!state.runId || state.refreshing || document.hidden) return;
  state.refreshing = true;
  try {
    const response = await fetch(`/api/run/${encodeURIComponent(state.runId)}`);
    if (!response.ok) return;
    const data = await response.json();
    if (data.error) return;
    state.data = data;
    state.index = buildIndex(data);
    if (state.selected && !state.index.actionById.has(state.selected)) {
      state.selected = null;
      overlay.classList.remove("show");
      drawer.classList.remove("show");
    }
    render();
    if (state.selected) renderDrawer();
  } finally {
    state.refreshing = false;
  }
}

function render() {
  configureStrategyPanels();
  renderHeader();
  renderStats();
  renderCurve();
  renderDelta();
  renderProtocol();
  renderArms();
  if (state.data.research) {
    renderResearch();
  } else {
    document.getElementById('research-panel').hidden = true;
    renderAllocation();
    renderNovelty();
    renderPolicyEvolution();
    renderSelection();
  }
  renderLineage();
  renderAttempts();
}

function configureStrategyPanels() {
  const memory = state.data.meta.method === "memory";
  const overview = document.getElementById("strategy-overview");
  document.getElementById("alloc-panel").style.display = "none";
  document.getElementById("novelty-panel").style.display = memory ? "" : "none";
  overview.style.display = memory ? "grid" : "none";
  overview.classList.toggle("memory-only", memory);
}

/* ---------- header & stats ---------- */

function renderHeader() {
  const { meta, batches } = state.data;
  document.getElementById("exp-name").textContent = meta.run_id;
  const started = batches.length ? new Date(batches[0].created_at) : null;
  document.getElementById("exp-sub").innerHTML =
    `${meta.batch_count} batches — ${esc(humanize(meta.environment || "environment not recorded"))}` +
    (started ? ` — started ${started.toLocaleString()}` : "") +
    ` — objective ${esc(humanize(meta.objective || "not recorded"))}` +
    ` — ${esc(metricLabel(meta.metric_name))} (${esc(meta.metric_direction)})`;
  document.getElementById("carrier-pill").textContent = policyLabel(meta.method);
  document.getElementById("carrier-pill").title = policyLabel(meta.method, true);
  const actors = document.getElementById("actor-summary");
  clear(actors);
  const proposer = meta.proposer || {};
  const worker = meta.worker || {};
  const actorFacts = [
    proposer.model || proposer.provider
      ? ["Proposer", [proposer.provider, proposer.model, proposer.provider_only].filter(Boolean).join(" — ")]
      : null,
    worker.model || worker.provider
      ? ["Worker", [worker.provider, worker.model, worker.provider_only].filter(Boolean).join(" — ")]
      : worker.runner_id
        ? ["Worker", `${humanize(worker.runner_id)} — model not recorded`]
        : null,
    meta.evaluator ? ["Evaluator", humanize(meta.evaluator)] : null,
    meta.solver?.model ? ["Solver", [meta.solver.model,meta.solver.provider_only,
      meta.solver.task_timeout_seconds ? `${meta.solver.task_timeout_seconds}s per task` : null].filter(Boolean).join(' — ')] : null,
  ].filter(Boolean);
  for (const [label, value] of actorFacts) {
    actors.appendChild(el("span", { html: `${esc(label)}: <strong>${esc(value)}</strong>` }));
  }
  const inFlight = state.data.actions.filter((action) =>
    ["running", "evaluating"].includes(action.lifecycle_stage),
  ).length;
  const blocked = state.data.actions.filter((action) => action.lifecycle_stage === "blocked").length;
  const status = document.getElementById("run-status");
  const lastBatch = batches.length ? batches[batches.length - 1] : null;
  const interrupted = lastBatch && ["interrupted", "failed"].includes(lastBatch.status);
  const archived = meta.run_storage_state === "aborted archive";
  status.textContent = archived
    ? "archived after stop"
    : interrupted
    ? humanize(lastBatch.status)
    : blocked
      ? `${blocked} blocked`
    : inFlight
      ? `${inFlight} running`
      : "ready";
  status.className = `pill${archived || interrupted || blocked ? " problem" : inFlight ? "" : " active"}`;
  const timestamps = [
    ...batches.map((batch) => batch.created_at),
    ...state.data.actions.map((action) => action.evaluation.completed_at || action.created_at),
  ].filter(Boolean);
  const latest = timestamps.length
    ? new Date(Math.max(...timestamps.map((value) => new Date(value).getTime())))
    : null;
  document.getElementById("refresh-time").textContent = latest
    ? `latest record ${latest.toLocaleString()}`
    : "no persisted activity yet";

  const alert = document.getElementById("run-alert");
  const failure = interrupted
    ? [...(state.data.planning_failures || [])].reverse().find((item) => item.batch_id === lastBatch.batch_id)
    : null;
  const warnings = meta.data_warnings || [];
  if (interrupted) {
    alert.className = "run-alert";
    const counts = {};
    for (const attempt of failure?.attempts || []) {
      counts[attempt.status] = (counts[attempt.status] || 0) + 1;
    }
    const detail = Object.entries(counts)
      .map(([key, value]) => `${value} ${humanize(key)}`)
      .join(" — ");
    alert.textContent = `${lastBatch.batch_id.replace(/^batch-?/, "Batch ")} was interrupted` +
      (failure ? `: ${humanize(failure.failure_kind)}` : "") +
      (detail ? ` — ${detail}` : "");
    alert.hidden = false;
  } else if (warnings.length) {
    alert.className = "run-alert notice";
    alert.textContent = warnings.join(" ");
    alert.hidden = false;
  } else {
    alert.hidden = true;
  }
}

function statCard(label, value, sub, unit) {
  return el("div", { class: "stat" }, [
    el("div", { class: "label", text: label }),
    el("div", {
      class: "value",
      html: `${esc(value)}${unit ? `<span class="unit">${esc(unit)}</span>` : ""}`,
    }),
    el("div", { class: "sub", text: sub }),
  ]);
}

function renderStats() {
  const { summary, meta, actions, programs, curve } = state.data;
  const host = document.getElementById("stats");
  clear(host);
  const inheritedCount = state.data.prehistory?.actions?.length || 0;
  const warmAccepted = state.data.prehistory?.branches?.length || 0;

  const root = programs.find((program) => program.archive_seq === 0);
  const minimize = meta.metric_direction === "minimize";
  const best = programs.reduce((top, program) => {
    const value = program.primary_value;
    if (value === null || value === undefined) return top;
    if (top === null || (minimize ? value < top : value > top)) return value;
    return top;
  }, null);
  const measured = actions.filter((item) => item.evaluation.completed_at).length;
  const planned = actions.filter((item) => item.lifecycle_stage === "planned").length;
  const active = actions.filter((item) => ["running", "evaluating"].includes(item.lifecycle_stage)).length;
  const blocked = actions.filter((item) => item.lifecycle_stage === "blocked").length;
  const aborted = actions.filter((item) => item.lifecycle_stage === "aborted").length;
  const last = curve.length ? curve[curve.length - 1] : null;

  host.appendChild(
    statCard(
      "Attempt plan",
      inheritedCount + actions.length,
      [
        inheritedCount ? `${inheritedCount} inherited` : null,
        planned ? `${planned} waiting` : null,
        active ? `${active} active` : null,
        blocked ? `${blocked} blocked` : null,
        aborted ? `${aborted} stopped` : null,
        measured ? `${measured} measured` : null,
      ].filter(Boolean).join(" — ") || "No activity yet",
    ),
  );
  host.appendChild(
    statCard(
      warmAccepted ? "Archive programs" : "Added to archive",
      warmAccepted + summary.total_admitted,
      warmAccepted
        ? `${warmAccepted} accepted warm-starts — ${summary.total_admitted} added by this run`
        : measured
          ? `${pct(summary.total_admitted / measured)} of measured attempts`
          : "Waiting for measurements",
    ),
  );
  host.appendChild(
    statCard(
      "Current best",
      metricValue(best),
      metricLabel(state.data.meta.metric_name),
    ),
  );
  host.appendChild(
    statCard(
      "Recorded cost",
      `$${(summary.total_cost_usd || 0).toFixed(2)}`,
      last ? `${fmt(last.cumulative_elapsed_seconds, 0)}s elapsed` : "—",
    ),
  );
}

/* ---------- learning curve ---------- */

/* One mark per attempt: the challenger it produced against the parent it was
 * measured against, under the best-so-far envelope. */
function renderCurve() {
  const host = document.getElementById("chart-curve");
  clear(host);
  const actions = allAttempts();
  if (!actions.length) {
    host.appendChild(el("div", { class: "empty", text: "No attempts yet." }));
    return;
  }

  const W = host.clientWidth || 720;
  const H = 320;
  const pad = { top: 34, right: 16, bottom: 30, left: 46 };
  const plotW = W - pad.left - pad.right;
  const plotH = H - pad.top - pad.bottom;
  const bottom = pad.top + plotH;
  const canvas = svg("svg", {
    viewBox: `0 0 ${W} ${H}`,
    width: "100%",
    height: H,
  });

  const n = actions.length;
  const x = (i) => pad.left + (n === 1 ? plotW / 2 : (plotW * i) / (n - 1));
  const observedValues = [
    ...actions.map((action) =>
      action.evaluation.completed_at ? action.evaluation.child_primary_value : null,
    ),
    ...actions.map((action) => {
      const child = action.evaluation.child_primary_value;
      const delta = action.evaluation.primary_delta_mean;
      return action.evaluation.completed_at && typeof child === "number" && typeof delta === "number"
        ? child - delta
        : null;
    }),
    ...state.data.curve.map((point) => point.best_primary_value),
  ].filter((value) => typeof value === "number" && Number.isFinite(value));
  let lo = observedValues.length ? Math.min(...observedValues) : 0;
  let hi = observedValues.length ? Math.max(...observedValues) : 1;
  if (isRateMetric()) {
    lo = 0;
    hi = 1;
  } else {
    const spread = hi - lo || Math.abs(hi) * 0.2 || 1;
    lo -= spread * 0.08;
    hi += spread * 0.08;
  }
  const y = (v) => pad.top + plotH - plotH * ((v - lo) / (hi - lo));

  batchBands(
    canvas,
    x,
    pad.top,
    plotH,
    [...(state.data.prehistory?.batches || []), ...state.data.batches],
  );
  gridY(
    canvas,
    pad.left,
    W - pad.right,
    [0, 1, 2, 3, 4].map((step) => {
      const v = lo + ((hi - lo) * step) / 4;
      return { v, y: y(v) };
    }),
    metricValue,
  );
  axisX(canvas, n, x, H - 10);
  generationOverlay(canvas, x, pad.top, bottom, W - pad.right, { actions });

  /* Blue is the best measured challenger regardless of whether it entered the
   * archive. Archive/policy generations remain the separate green overlay. */
  let best = null;
  const envelope = [];
  actions.forEach((action, i) => {
    const value = action.evaluation.child_primary_value;
    if (
      action.evaluation.completed_at &&
      typeof value === "number" &&
      isBetter(value, best)
    ) {
      if (best !== null && best !== undefined) envelope.push([x(i), y(best)]);
      best = value;
      envelope.push([x(i), y(best)]);
    } else if (best !== null && best !== undefined) {
      envelope.push([x(i), y(best)]);
    }
  });
  if (envelope.length) {
    const d = envelope.map(([px, py], index) => `${index ? "L" : "M"} ${px} ${py}`).join(" ");
    canvas.appendChild(
      svg("path", {
        d: `${d} L ${envelope[envelope.length - 1][0]} ${bottom} L ${envelope[0][0]} ${bottom} Z`,
        fill: getCss("--accent"),
        opacity: 0.07,
      }),
    );
    canvas.appendChild(
      svg("path", {
        d,
        fill: "none",
        stroke: getCss("--accent"),
        "stroke-width": 2.2,
        "stroke-linejoin": "round",
      }),
    );
  }

  /* Plot only the challenger's persisted absolute readout.  The paired parent
   * measurement belongs in the paired-effect panel; an archived parent value
   * may come from a different evidence slice. */
  actions.forEach((action, i) => {
    const cx = x(i);
    const challenger = action.evaluation.child_primary_value;
    const hasChallenger = challenger !== null && challenger !== undefined;
    if (!hasChallenger) return;
    const delta = action.evaluation.primary_delta_mean;
    const pairedParent = typeof delta === "number" ? challenger - delta : null;
    if (pairedParent !== null) {
      canvas.appendChild(
        svg("line", {
          x1: cx,
          x2: cx,
          y1: y(pairedParent),
          y2: y(challenger),
          stroke: "#d8d5cd",
          "stroke-width": 1.2,
        }),
      );
      canvas.appendChild(
        svg("line", {
          x1: cx - 5,
          x2: cx + 5,
          y1: y(pairedParent),
          y2: y(pairedParent),
          stroke: "#b6b2a7",
          "stroke-width": 2.5,
        }),
      );
    }
    const admitted = displayAdmitted(action);
    canvas.appendChild(
      svg("circle", {
        cx,
        cy: y(challenger),
        r: 5,
        fill: admitted ? getCss("--green") : getCss("--red"),
        stroke: "#fff",
        "stroke-width": 1.4,
        style: "cursor:pointer",
        onclick: () => openDrawer(action.action_id),
        onmousemove: (event) =>
          showTip(event, `Attempt #${attemptNumber(action)} — ${esc(archiveVerdict(action).label)}`, [
            esc(readable(action.direction.failure_mechanism)),
            `challenger <b>${metricValue(challenger)}</b>` +
              (pairedParent === null ? "" : ` — paired parent ${metricValue(pairedParent)}`),
            `paired effect ${points(action.evaluation.primary_delta_mean)}`,
            `policy feedback: ${esc(action.effectiveness_label || "pending")}`,
          ]),
        onmouseleave: hideTip,
      }),
    );
  });

  host.appendChild(canvas);
  const inheritedScored = (state.data.prehistory?.actions || []).filter(
    (action) => action.evaluation.completed_at,
  ).length;
  const localScored = state.data.actions.filter(
    (action) => action.evaluation.completed_at,
  ).length;
  const scored = inheritedScored + localScored;
  document.getElementById("curve-note").textContent = scored
    ? `${inheritedScored} inherited + ${localScored} current measured attempts — shaded bands = planning batches`
    : "waiting for first scores…";
}

/* ---------- admission signal ---------- */

/* The paired effect and its own conservative lower bound.  Policy
 * effectiveness and archive admission stay separate labels. */
function renderDelta() {
  const host = document.getElementById("chart-delta");
  clear(host);
  const actions = state.data.actions.filter(
    (item) => item.evaluation.primary_delta_mean !== null,
  );
  if (!actions.length) {
    host.appendChild(el("div", { class: "empty", text: "No measurements yet." }));
    return;
  }

  const W = host.clientWidth || 420;
  const H = 300;
  const pad = { top: 14, right: 16, bottom: 30, left: 46 };
  const plotW = W - pad.left - pad.right;
  const plotH = H - pad.top - pad.bottom;
  const canvas = svg("svg", {
    viewBox: `0 0 ${W} ${H}`,
    width: "100%",
    height: H,
  });

  const values = [];
  for (const action of actions) {
    values.push(action.evaluation.primary_delta_mean);
    if (action.evaluation.primary_delta_lower !== null) {
      values.push(action.evaluation.primary_delta_lower);
    }
  }
  const span = Math.max(0.05, ...values.map(Math.abs)) * 1.15;
  const n = actions.length;
  const x = (i) => pad.left + (n === 1 ? plotW / 2 : (plotW * i) / (n - 1));
  const y = (v) => pad.top + plotH / 2 - (plotH / 2) * (v / span);

  gridY(
    canvas,
    pad.left,
    W - pad.right,
    [span, span / 2, 0, -span / 2, -span].map((v) => ({ v, y: y(v) })),
    (v) => points(v, 0),
  );
  canvas.appendChild(
    svg("line", {
      x1: pad.left,
      x2: W - pad.right,
      y1: y(0),
      y2: y(0),
      stroke: "#8f94a0",
      "stroke-dasharray": "4 3",
    }),
  );

  const every = n > 60 ? 10 : n > 34 ? 5 : n > 18 ? 2 : 1;
  actions.forEach((action, i) => {
    const ordinal = attemptNumber(action);
    const mean = action.evaluation.primary_delta_mean;
    if (i % every === 0 || i === n - 1) {
      canvas.appendChild(
        svg("text", {
          x: x(i),
          y: H - 10,
          text: ordinal,
          fill: "#a3a7b0",
          "font-size": 10,
          "text-anchor": "middle",
        }),
      );
    }
    const lower = action.evaluation.primary_delta_lower;
    const color = action.evaluation.admitted ? getCss("--green") : getCss("--red");
    if (lower !== null && lower !== undefined) {
      canvas.appendChild(
        svg("line", {
          x1: x(i),
          y1: y(mean),
          x2: x(i),
          y2: y(lower),
          stroke: color,
          "stroke-width": 1.6,
          opacity: 0.55,
        }),
      );
      canvas.appendChild(
        svg("line", {
          x1: x(i) - 4,
          x2: x(i) + 4,
          y1: y(lower),
          y2: y(lower),
          stroke: color,
          "stroke-width": 1.6,
          opacity: 0.55,
        }),
      );
    }
    canvas.appendChild(
      svg("circle", {
        cx: x(i),
        cy: y(mean),
        r: 5,
        fill: color,
        stroke: "#fff",
        "stroke-width": 1.4,
        style: "cursor:pointer",
        onclick: () => openDrawer(action.action_id),
        onmousemove: (event) =>
          showTip(event, `Attempt #${ordinal} — ${esc(action.evaluation.admission_fate || "")}`, [
            `paired mean <b>${points(mean, 2)}</b>`,
            lower === null || lower === undefined
              ? "no conservative lower bound recorded"
              : `conservative lower ${points(lower, 2)}`,
            `policy feedback: ${esc(action.effectiveness_label || "not recorded")}`,
            `${esc(action.evaluation.validity || "")} — ${esc(action.evaluation.attribution || "")}`,
          ]),
        onmouseleave: hideTip,
      }),
    );
  });

  host.appendChild(canvas);
  const withBound = actions.filter(
    (item) => item.evaluation.primary_delta_lower !== null,
  ).length;
  document.getElementById("delta-note").textContent = withBound
    ? `${isRateMetric() ? "effect in percentage points" : "effect in metric units"} — whisker = lower bound`
    : `${isRateMetric() ? "effect in percentage points" : "effect in metric units"} — no lower bound recorded`;
}

/* ---------- evidence protocol ---------- */

/* A / B / C is one evidence contract, not three counters.  The three sizes come
 * from three different records, so each segment says where its number is from
 * and an unregistered tier stays blank instead of showing a zero. */
function renderProtocol() {
  const protocol = state.data.protocol || { tiers: [] };
  const design = protocol.estimator_design || {};
  const host = document.getElementById("tier-bar");
  clear(host);

  const bar = el("div", { class: "tierbar" });
  for (const tier of protocol.tiers) {
    const size = tier.cell_count;
    bar.appendChild(
      el(
        "div",
        {
          class: `seg ${tier.tier.toLowerCase()}${size ? "" : " unknown"}`,
          onmousemove: (event) =>
            showTip(event, `Tier ${tier.tier} — ${esc(tier.role)}`, [
              size ? `${size} cells` : "size not recorded in this run",
              `source: ${esc(tier.source)}`,
              tier.state ? `state: ${esc(tier.state)}` : null,
            ]),
          onmouseleave: hideTip,
        },
        [
          el("div", { class: "t", text: `${tier.tier} — ${tier.role}` }),
          el("div", { class: "n", text: size ?? "—" }),
          el("div", {
            class: "r",
            text: tier.tier === "B" && design.sample_size && design.population
              ? `wave ${design.sample_size} of ${design.population} — paired x${design.replicates || 1}`
              : tier.tier === "C" && !tier.state && !size
                ? "not configured"
                : tier.state || tier.source,
          }),
        ],
      ),
    );
  }
  host.appendChild(bar);
  const visibleBriefs = state.data.meta.visible_a_evidence || [];
  const rootProgram = state.data.programs.find((program) => program.archive_seq === 0);
  if (visibleBriefs.length) {
    const baseline = el("div", { class: "baseline-cells" }, [
      el("div", { class: "baseline-label" }, [
        el("strong", { text: "A baseline cells" }),
        el("span", { text: "Root program results used as visible development evidence" }),
      ]),
    ]);
    const cells = el("div", { class: "baseline-cell-grid" });
    for (const brief of visibleBriefs) {
      const result = rootProgram?.cell_results?.[brief.cell_id];
      cells.appendChild(
        el("button", {
          class: `baseline-cell ${result === true ? "pass" : result === false ? "fail" : "pending"}`,
          type: "button",
          text: brief.title,
          "aria-label": `${brief.title}: ${result === true ? "passed" : result === false ? "failed" : "not recorded"}`,
          onmousemove: (event) =>
            showTip(event, esc(brief.title), [
              `Root baseline: <b>${result === true ? "passed" : result === false ? "failed" : "not recorded"}</b>`,
              brief.observed_failure ? `Observed failure: ${esc(humanize(brief.observed_failure))}` : null,
              "Open the visible task list below for the full task statement.",
            ]),
          onmouseleave: hideTip,
        }),
      );
    }
    baseline.appendChild(cells);
    host.appendChild(baseline);
    const catalogue = el("details", { class: "evidence-catalog" }, [
      el("summary", { text: `View the ${visibleBriefs.length} visible A-tier tasks` }),
    ]);
    for (const brief of visibleBriefs) {
      catalogue.appendChild(
        el("details", { class: "evidence-brief" }, [
          el("summary", { text: brief.title || cellLabel(brief.cell_id) }),
          el("div", { class: "prose", text: brief.statement }),
          brief.observed_failure
            ? hint(`Observed failure: ${humanize(brief.observed_failure)}`)
            : null,
        ]),
      );
    }
    host.appendChild(catalogue);
  }

  if (design.sample_size && design.population) {
    const weighted = design.information_weighting?.enabled;
    const floor = design.information_weighting?.floor;
    host.appendChild(
      el("article", { class: "b-wave" }, [
        el("div", {
          class: "b-wave-title",
          text: `B wave — ${design.sample_size} of ${design.population} tasks, paired x${design.replicates || 1}`,
        }),
        el("p", {
          text:
            `Each completed challenger gets its own wave, selected before either side runs. ` +
            `The parent and challenger are evaluated on the same ${design.sample_size} tasks, ` +
            `${design.replicates || 1} time${(design.replicates || 1) === 1 ? "" : "s"} per side.`,
        }),
        weighted
          ? el("p", {
              text:
                `Sampling favors tasks that better separate programs` +
                `${typeof floor === "number" ? `, with a ${floor} weight floor so every B task remains reachable` : ""}.`,
            })
          : null,
      ]),
    );
  }

  const notes = document.getElementById("tier-notes");
  clear(notes);
  const facts = [
    ...(protocol.estimator ? [["estimator", humanize(protocol.estimator)]] : []),
    ["evaluation records", protocol.measured_actions],
    [
      "sealed C",
      protocol.sealed_state
        ? `${protocol.sealed_state}${protocol.sealed_expected_blocks ? ` — ${protocol.sealed_expected_blocks} blocks` : ""}`
        : "not configured for this run",
    ],
  ];
  for (const [label, value] of facts) {
    notes.appendChild(el("span", { html: `${esc(label)}: <b>${esc(value)}</b>` }));
  }
  const sealed = protocol.tiers.find((tier) => tier.tier === "C");
  document.getElementById("protocol-note").textContent = protocol.sealed_state
    ? "The final holdout is registered; its task list remains private"
    : sealed && sealed.cell_count
      ? "The final holdout is recorded but has no lifecycle state"
      : "No final holdout was configured for this run";
}

/* ---------- search allocation ---------- */

/* Where each batch's attempts went: exploring a brand-new mechanism, or another
 * realization of one the run already knows. */
function renderAllocation() {
  const host = document.getElementById("chart-alloc");
  clear(host);
  const { data, index } = state;
  const hasSemanticArms = data.meta.method === "ucb";
  if (document.getElementById("alloc-panel").style.display === "none") {
    clear(document.getElementById("alloc-legend"));
    return;
  }
  document.getElementById("alloc-intro").textContent = hasSemanticArms
    ? "Each row is one planning batch and each mark is one attempted change. Colour is policy feedback; a green outer ring means the resulting program was added to the archive. Diamonds explore a new mechanism; squares reuse one."
    : "Each row is one planning batch and each dot is one attempted change selected by the Textual policy. Colour is policy feedback; a green outer ring means the resulting program was added to the archive.";
  const batches = data.batches;
  if (!batches.length) {
    host.appendChild(el("div", { class: "empty", text: "No batches yet." }));
    return;
  }

  const actionsByBatch = new Map();
  for (const action of data.actions) {
    if (!actionsByBatch.has(action.batch_id)) actionsByBatch.set(action.batch_id, []);
    actionsByBatch.get(action.batch_id).push(action);
  }

  const rowH = 32;
  const left = 62;
  const maxCount = Math.max(
    ...batches.map((batch) => (actionsByBatch.get(batch.batch_id) || []).length),
    1,
  );
  const naturalWidth = left + maxCount * 32 + 150;
  const W = Math.max(host.clientWidth || 620, naturalWidth);
  const H = batches.length * rowH + 22;
  const canvas = svg("svg", { width: W, height: H });
  const cell = Math.min(38, (W - left - 150) / maxCount);

  batches.forEach((batch, row) => {
    const y = 18 + row * rowH;
    canvas.appendChild(
      svg("text", {
        x: 6,
        y: y + 4,
        text: batch.batch_id.replace(/^batch-?/, "batch "),
        fill: "var(--muted)",
        "font-size": 10,
      }),
    );
    const actions = actionsByBatch.get(batch.batch_id) || [];
    actions.forEach((action, column) => {
      const discover = action.slot_kind === "discover";
      const label = action.effectiveness_label;
      const color =
        label === "success"
          ? "var(--green)"
          : label === "failure"
            ? "var(--red)"
            : label === "skip"
              ? "var(--amber)"
              : action.lifecycle_stage === "blocked"
                ? "var(--red)"
                : "#b8b8b2";
      const cx = left + column * cell + cell / 2;
      const group = svg("g", {
        transform: `translate(${cx},${y})`,
        style: "cursor:pointer",
        onclick: () => openDrawer(action.action_id),
        onmousemove: (event) =>
          showTip(event, `Attempt #${attemptNumber(action)}`, [
            esc(readable(action.direction.failure_mechanism)),
            hasSemanticArms
              ? `${discover ? "new mechanism" : "reused mechanism"} — match ${esc(action.match_kind || "—")}`
              : "ranked by the Textual policy",
            `${esc(lifecycleLabel(action))}${label ? ` — ${esc(label)}` : ""}${action.evaluation.admitted ? " — admitted" : ""}`,
          ]),
        onmouseleave: hideTip,
      });
      if (action.evaluation.admitted) {
        group.appendChild(
          svg("circle", { r: 9, fill: "none", stroke: "var(--green)", "stroke-width": 1.5 }),
        );
      }
      group.appendChild(
        !hasSemanticArms
          ? svg("circle", { r: 5.4, fill: color, opacity: 0.88 })
          : discover
          ? svg("rect", {
              x: -4.5,
              y: -4.5,
              width: 9,
              height: 9,
              transform: "rotate(45)",
              fill: color,
              opacity: 0.85,
            })
          : svg("rect", { x: -5, y: -5, width: 10, height: 10, rx: 2, fill: color, opacity: 0.88 }),
      );
      group.appendChild(
        svg("circle", {
          r: 11,
          fill: "transparent",
          stroke: action.action_id === state.selected ? "var(--ink)" : "none",
        }),
      );
      canvas.appendChild(group);
    });
    const failures = index.failuresByBatch.get(batch.batch_id);
    const admitted = actions.filter((item) => item.evaluation.admitted).length;
    const active = actions.filter((item) => ["running", "evaluating"].includes(item.lifecycle_stage)).length;
    const planned = actions.filter((item) => item.lifecycle_stage === "planned").length;
    const blocked = actions.filter((item) => item.lifecycle_stage === "blocked").length;
    const stopped = actions.filter((item) => item.lifecycle_stage === "aborted").length;
    const completed = actions.filter((item) => item.lifecycle_stage === "completed").length;
    const summary = !actions.length && failures
      ? `${failures} planning round failed`
      : `${completed} completed` +
        (active ? ` — ${active} active` : "") +
        (planned ? ` — ${planned} waiting` : "") +
        (blocked ? ` — ${blocked} blocked` : "") +
        (stopped ? ` — ${stopped} stopped` : "") +
        (admitted ? ` — ${admitted} admitted` : "") +
        (failures ? ` — ${failures} empty round(s)` : "");
    canvas.appendChild(
      svg("text", {
        x: left + Math.max(actions.length, 1) * cell + 10,
        y: y + 4,
        text: summary,
        fill: admitted ? "var(--ink)" : "var(--faint)",
        "font-size": 10.5,
      }),
    );
  });

  host.appendChild(canvas);
  const legend = document.getElementById("alloc-legend");
  clear(legend);
  const entries = [
    ["var(--green)", "success"],
    ["var(--red)", "failure"],
    ["var(--amber)", "skip"],
    ...(hasSemanticArms ? [[null, "◆ new mechanism — ■ reused mechanism"]] : []),
    [null, "○ green ring = added to archive"],
  ];
  for (const [color, label] of entries) {
    const item = el("span", { class: "key" });
    if (color) item.appendChild(el("span", { class: "swatch", style: { background: color } }));
    item.appendChild(document.createTextNode(label));
    legend.appendChild(item);
  }

  const discovers = data.actions.filter((item) => item.slot_kind === "discover").length;
  const newArms = data.actions.filter((item) => item.match_kind === "new").length;
  const pending = data.actions.filter((item) => item.match_kind === "pending_new").length;
  document.getElementById("alloc-note").textContent = !data.actions.length
    ? ""
    : hasSemanticArms
      ? `${discovers} discovery slots — ${newArms} new mechanisms${pending ? ` — ${pending} joined this batch` : ""}`
      : `${batches.length} batches — Textual-ranked attempts`;
}

/* ---------- direction novelty ---------- */

/* Every proposed direction is embedded and compared with the ones the run has
 * already executed.  That nearest-neighbour distance is what decides whether an
 * attempt opens a new mechanism or is another realization of a known one, so
 * plotting the distances shows whether the proposer is repeating itself and
 * where the run's own decision boundary actually fell. */
function renderNovelty() {
  const host = document.getElementById("chart-novelty");
  clear(host);
  if (document.getElementById("novelty-panel").style.display === "none") return;
  const scored = state.data.actions
    .map((action, i) => ({ action, i, d: action.match_distance }))
    .filter((item) => typeof item.d === "number");
  const note = document.getElementById("novelty-note");
  const title = document.getElementById("novelty-title");
  const intro = document.getElementById("novelty-intro");
  if (!scored.length) {
    // a memory carrier keeps no semantic index; its counterpart is the document
    title.textContent = "Policy Memory used for the latest batch";
    intro.textContent = "These are the concrete guidance rules Textual Policy Learning read before choosing a parent and proposing directions for the latest batch. The collapsed history below shows how those rules changed over time.";
    renderMemory(host, note);
    return;
  }
  title.textContent = "Direction similarity";
  intro.textContent = "Horizontal position is cosine distance from the nearest executed direction. Vertical stacking only separates overlapping dots.";

  const W = host.clientWidth || 420;
  const H = 218;
  const pad = { left: 16, right: 16, top: 22, bottom: 42 };
  const plotW = W - pad.left - pad.right;
  const canvas = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H });
  const max = Math.max(1, ...scored.map((item) => item.d));
  const x = (d) => pad.left + (d / max) * plotW;
  const baseline = H - pad.bottom;

  canvas.appendChild(
    svg("line", {
      x1: pad.left,
      x2: pad.left + plotW,
      y1: baseline,
      y2: baseline,
      stroke: "#d8d5cd",
    }),
  );
  for (const tick of [0, 0.25, 0.5, 0.75, 1]) {
    const value = tick * max;
    canvas.appendChild(
      svg("line", {
        x1: x(value),
        x2: x(value),
        y1: pad.top,
        y2: baseline + 4,
        stroke: "#f0efe9",
      }),
    );
    canvas.appendChild(
      svg("text", {
        x: x(value),
        y: baseline + 17,
        text: value.toFixed(2),
        fill: "#a3a7b0",
        "font-size": 10,
        "text-anchor": "middle",
      }),
    );
  }

  /* Nudge overlapping points up into rows so a dense cluster stays countable
   * instead of collapsing into one dot. */
  const ordered = [...scored].sort((a, b) => a.d - b.d);
  const rowsNeeded = [];
  for (const item of ordered) {
    const cx = x(item.d);
    let row = 0;
    while (rowsNeeded[row] !== undefined && cx - rowsNeeded[row] < 11) row += 1;
    rowsNeeded[row] = cx;
    item.row = row;
  }
  const step = Math.min(12, (baseline - pad.top - 9) / Math.max(rowsNeeded.length, 1));
  const occupied = [];
  for (const item of ordered) {
    const cx = x(item.d);
    const row = item.row;
    occupied[row] = cx;
    const cy = baseline - 9 - row * step;
    const isNew = item.action.match_kind === "new";
    canvas.appendChild(
      svg("circle", {
        cx,
        cy: Math.max(pad.top, cy),
        r: 5,
        fill: isNew ? getCss("--violet") : getCss("--accent"),
        "fill-opacity": 0.85,
        stroke: "#fff",
        "stroke-width": 1,
        style: "cursor:pointer",
        onclick: () => openDrawer(item.action.action_id),
        onmousemove: (event) =>
          showTip(event, `Attempt #${attemptNumber(item.action)} — ${esc(item.action.match_kind)}`, [
            esc(readable(item.action.direction.failure_mechanism)),
            `nearest executed direction <b>${fmt(item.d, 4)}</b> away`,
            isNew
              ? "far enough to open a new mechanism"
              : `another realization of arm ${esc(tail(item.action.arm_id))}`,
          ]),
        onmouseleave: hideTip,
      }),
    );
  }

  canvas.appendChild(
    svg("text", {
      x: pad.left + plotW / 2,
      y: H - 4,
      text: "cosine distance to the nearest executed direction",
      fill: "#c3c1ba",
      "font-size": 9.5,
      "text-anchor": "middle",
    }),
  );
  host.appendChild(canvas);
  const allNew = state.data.actions.filter((item) => item.match_kind === "new").length;
  const allReused = state.data.actions.filter((item) => item.match_kind === "existing").length;
  const missing = state.data.actions.filter(
    (item) => ["new", "existing"].includes(item.match_kind) && typeof item.match_distance !== "number",
  ).length;
  note.textContent = `${allNew} new — ${allReused} reused${missing ? ` — ${missing} without distance` : ""}`;
}

/* The memory carrier rewrites one document each batch, and that document is the
 * whole of what it carries forward.  Listing the mechanisms it chose to keep
 * shows whether its attention settled or kept drifting. */
function renderMemory(host, note) {
  const written = state.data.batches.filter((batch) => batch.memory_content);
  if (!written.length) {
    note.textContent = "";
    host.appendChild(
      el("div", { class: "empty", text: "This run has no saved Policy Memory." }),
    );
    return;
  }
  const last = written[written.length - 1];
  const latestRules = parseMemoryRules(last.memory_content);
  const priorRules = written.length > 1
    ? parseMemoryRules(written[written.length - 2].memory_content)
    : [];
  const priorByFailure = new Map(priorRules.map((rule) => [rule.failure, rule]));
  const latestByFailure = new Map(latestRules.map((rule) => [rule.failure, rule]));
  const addedRules = latestRules.filter((rule) => !priorByFailure.has(rule.failure));
  const removedRules = priorRules.filter((rule) => !latestByFailure.has(rule.failure));
  note.textContent = `${last.label || last.batch_id.replace(/^batch-?/, "Batch ")} uses ${latestRules.length} saved rules`;
  if (!latestRules.length) {
    host.appendChild(el("div", { class: "empty", text: "The latest Policy Memory contains no conditional guidance." }));
  } else {
    const list = el("div", { class: "memory-rules" });
    latestRules.forEach((rule, index) => {
      let target = rule.target;
      try {
        const parsed = JSON.parse(rule.target);
        if (Array.isArray(parsed)) {
          target = parsed.map((item) => {
            const definitions = item.definitions || item.symbols || [];
            return [item.file, ...definitions].filter(Boolean).join(", ");
          }).join("; ");
        }
      } catch {}
      list.appendChild(
        el("article", { class: "memory-rule" }, [
          el("h4", { text: `Rule ${index + 1}: ${rule.failure || "Unnamed failure pattern"}` }),
          rule.applies ? el("p", { text: `When this applies: ${rule.applies}` }) : null,
          rule.response ? el("p", { text: `Recommended change: ${rule.response}` }) : null,
          target ? el("p", { text: `Target: ${target}` }) : null,
          rule.evidence ? el("p", { text: `Evidence basis: ${humanize(rule.evidence)}` }) : null,
        ]),
      );
    });
    host.appendChild(list);
  }

  if (written.length > 1) {
    const history = el("details", { class: "technical" });
    history.appendChild(el("summary", { text: "View changes to saved guidance" }));
    const changes = el("div", { class: "memory-change" });
    if (!addedRules.length && !removedRules.length) {
      changes.appendChild(el("p", { text: "No rules changed since the previous batch." }));
    }
    if (addedRules.length) {
      changes.appendChild(el("strong", { text: "Added rules" }));
      for (const rule of addedRules) changes.appendChild(el("p", { text: rule.failure }));
    }
    if (removedRules.length) {
      changes.appendChild(el("strong", { text: "Removed rules" }));
      for (const rule of removedRules) changes.appendChild(el("p", { text: rule.failure }));
    }
    history.appendChild(changes);
    host.appendChild(history);
  }
}

/* The carrier itself changes at batch barriers.  This compact timeline shows
 * the state that each batch started from, rather than treating the selected
 * actions as if they were the policy state. */
function renderPolicyEvolution() {
  const panel = document.getElementById("policy-panel");
  const host = document.getElementById("chart-policy");
  const title = document.getElementById("policy-title");
  const note = document.getElementById("policy-note");
  const intro = document.getElementById("policy-intro");
  clear(host);
  const { batches, actions, meta } = state.data;
  panel.style.display = meta.method === "memory" ? "none" : "";
  if (meta.method === "memory") {
    clear(host);
    return;
  }
  panel.parentNode.insertBefore(panel, document.getElementById("arms-panel"));
  const list = el("div", { class: "policy-timeline" });

  if (meta.method === "ucb") {
    title.textContent = policyLabel(meta.method, true);
    note.textContent = `${batches.length} batch snapshots`;
    intro.textContent = "Known mechanisms come from the frozen registry at batch start; allocation and feedback summarize what happened before the next update.";
    for (const batch of batches) {
      const batchActions = actions.filter((action) => action.batch_id === batch.batch_id);
      const discover = batchActions.filter((action) => action.slot_kind === "discover").length;
      const reused = batchActions.filter((action) => action.slot_kind === "existing").length;
      const successes = batchActions.filter((action) => action.effectiveness_label === "success").length;
      const failures = batchActions.filter((action) => action.effectiveness_label === "failure").length;
      const skips = batchActions.filter((action) => action.effectiveness_label === "skip").length;
      const row = el("div", { class: "policy-row" }, [
        el("div", { class: "policy-batch", text: batch.label || batch.batch_id.replace(/^batch-?/, "Batch ") }),
        el("div", { class: "policy-facts" }, [
          el("span", { html: `<strong>${batch.arm_ids.length}</strong> known mechanisms` }),
          el("span", { html: `<strong>${discover}</strong> discover — <strong>${reused}</strong> reuse` }),
          el("span", { html: `<strong>${successes}</strong> success — <strong>${failures}</strong> failure — <strong>${skips}</strong> skip` }),
          batch.status === "interrupted" ? el("span", { class: "delta-neg", text: "interrupted" }) : null,
        ]),
      ]);
      list.appendChild(row);
    }
  }
  if (!list.childElementCount) {
    host.appendChild(el("div", { class: "empty", text: "No carrier snapshots were persisted." }));
    return;
  }
  let history = null;
  if (list.childElementCount > 6) {
    const earlier = [...list.children].slice(0, -6);
    const historyBody = el("div", { class: "policy-timeline more-list-body" });
    for (const row of earlier) historyBody.appendChild(row);
    history = el("details", { class: "more-list" }, [
      el("summary", { text: `Show ${earlier.length} earlier policy snapshots` }),
      historyBody,
    ]);
  }
  host.appendChild(list);
  if (history) host.appendChild(history);
}

/* ---------- mechanism reuse ---------- */

/* Each arm is a family of directions the semantic index judged equivalent.  The
 * bar shows how the attempts drawn from it turned out — the run's evidence that
 * the family is worth returning to. */
function renderArms() {
  const host = document.getElementById("chart-arms");
  clear(host);
  const actions = state.data.actions.filter((item) => item.arm_id);
  const note = document.getElementById("arm-note");
  // only a bandit carrier keeps arms; a memory run has nothing to show here
  document.getElementById("arms-panel").style.display = actions.length ? "" : "none";
  if (!actions.length) {
    note.textContent = "";
    return;
  }

  const arms = new Map();
  for (const action of actions) {
    if (!arms.has(action.arm_id)) {
      arms.set(action.arm_id, {
        id: action.arm_id,
        name: readable(action.direction.failure_mechanism),
        implementation: readable(action.direction.implementation_direction),
        success: 0,
        failure: 0,
        skip: 0,
        pending: 0,
        admitted: 0,
        latestActionId: action.action_id,
      });
    }
    const arm = arms.get(action.arm_id);
    if (["success", "failure", "skip"].includes(action.effectiveness_label)) {
      arm[action.effectiveness_label] += 1;
    } else {
      arm.pending += 1;
    }
    if (action.evaluation.admitted) arm.admitted += 1;
    arm.latestActionId = action.action_id;
  }
  const rows = [...arms.values()].sort(
    (a, b) => b.success + b.failure + b.skip + b.pending - (a.success + a.failure + a.skip + a.pending),
  );
  const list = el("div");
  for (const arm of rows) {
    const total = arm.success + arm.failure + arm.skip + arm.pending;
    const stack = el("div", { class: "outcome-stack", title: `${arm.success} success — ${arm.failure} failure — ${arm.skip} skip — ${arm.pending} pending` });
    for (const key of ["success", "failure", "skip"]) {
      if (!arm[key]) continue;
      stack.appendChild(el("span", { class: key, style: { width: `${(arm[key] / total) * 100}%` } }));
    }
    if (arm.pending) {
      stack.appendChild(el("span", { class: "pending", style: { width: `${(arm.pending / total) * 100}%` } }));
    }
    list.appendChild(
      el("div", { class: "mechanism-row", onclick: () => openDrawer(arm.latestActionId) }, [
        el("div", {}, [
          el("div", { class: "mechanism-name", text: arm.name }),
          el("div", { class: "mechanism-title", text: arm.implementation }),
        ]),
        stack,
        el("div", { class: "mechanism-summary", text: `${total} planned — ${arm.pending} pending — ${arm.admitted} admitted` }),
      ]),
    );
  }
  host.appendChild(list);
  if (list.childElementCount > 5) {
    const rest = [...list.children].slice(5);
    const body = el("div", { class: "more-list-body" });
    for (const row of rest) body.appendChild(row);
    host.appendChild(
      el("details", { class: "more-list" }, [
        el("summary", { text: `Show ${rest.length} more mechanism families` }),
        body,
      ]),
    );
  }
  note.textContent = `${rows.length} mechanism families — colours = policy feedback`;
}

/* ---------- parent selection ---------- */

/* Comparable parent-choice categories.  Raw rank lanes are not comparable
 * across attempts because every slate has a different size and membership. */
function renderSelection() {
  const host = document.getElementById("chart-selection");
  clear(host);
  const { data, index } = state;
  const legend = document.getElementById("sel-legend");
  clear(legend);
  if (!data.actions.length) {
    host.appendChild(el("div", { class: "empty", text: "No attempts yet." }));
    return;
  }

  const choiceFor = (action) => {
    const decision = index.decisionByAction.get(action.action_id);
    if (!decision || !decision.parent_options.length) return null;
    const eligible = decision.parent_options.filter((option) => option.eligible);
    const ordered = [...eligible].sort(
      (a, b) => (b.index ?? -Infinity) - (a.index ?? -Infinity),
    );
    const at = ordered.findIndex((option) => option.selected);
    return at < 0
      ? null
      : {
          category: action.parent_id === "root" ? "root" : at === 0 ? "top" : "other",
          rank: at + 1,
          slate: ordered.length,
          option: ordered[at],
          probability: decision.parent_conditional_probability,
          tied: ordered.length > 1 && ordered.every((option) => option.index === ordered[0].index),
        };
  };

  const placed = data.actions.map((action, i) => ({ action, i, ...(choiceFor(action) || {}) }));
  const counts = {
    top: placed.filter((item) => item.category === "top").length,
    other: placed.filter((item) => item.category === "other").length,
    root: placed.filter((item) => item.category === "root").length,
  };
  document.getElementById("sel-note").textContent = "Recent parent selection decisions";
  host.appendChild(
    el("div", { class: "selection-stats" }, [
      el("span", { class: "selection-stat", html: `Top ranked <strong>${counts.top}</strong>` }),
      el("span", { class: "selection-stat", html: `Alternative <strong>${counts.other}</strong>` }),
      el("span", { class: "selection-stat", html: `Starting program <strong>${counts.root}</strong>` }),
    ]),
  );

  const tableFor = (items) => {
    const table = el("table", { class: "selection-table" });
    table.innerHTML = "<thead><tr><th>Attempt</th><th>Selected parent</th><th>Why selected</th><th>Child result</th></tr></thead>";
    const body = el("tbody");
    for (const item of items) {
      const action = item.action;
      const isRootParent = action.parent_label === "Root" || item.option?.label === "Root" || item.option?.archive_seq === 0;
      const source = isRootParent ? null : index.actionByCommit.get(action.parent_commit_sha);
      const parentName = source
        ? `Attempt #${attemptNumber(source)}: ${readable(source.direction.failure_mechanism)}`
        : "Starting program";
      const parentSub = source
        ? `${source.batch_id.replace(/^batch-?/, "Batch ")}. Parent score ${metricValue(item.option?.primary_value)}`
        : `Original program. Parent score ${metricValue(item.option?.primary_value)}`;
      let reason;
      if (item.tied) reason = "Eligible parents had equal policy scores. The recorded tie break selected this parent.";
      else if (item.category === "top" || item.category === "root") reason = `Top ranked among ${item.slate} eligible programs.`;
      else reason = `Rank ${item.rank} among ${item.slate} eligible programs.`;
      if (typeof item.probability === "number") reason += ` Selection probability ${pct(item.probability)}.`;
      const verdict = archiveVerdict(action);
      const row = el("tr", { style: { cursor: "pointer" }, onclick: () => openDrawer(action.action_id) });
      row.innerHTML = `<td>Attempt #${attemptNumber(action)}</td>` +
        `<td><div class="selection-parent">${esc(parentName)}</div><div class="selection-sub">${esc(parentSub)}</div></td>` +
        `<td>${esc(reason)}</td>` +
        `<td><span class="badge ${verdict.className}">${esc(verdict.label)}</span>` +
        `<div class="selection-sub">${esc(lifecycleLabel(action))}</div>` +
        `<div class="selection-sub">Score ${metricValue(action.evaluation.child_primary_value)}. Change ${points(action.evaluation.primary_delta_mean)}</div></td>`;
      body.appendChild(row);
    }
    table.appendChild(body);
    return table;
  };

  const ordered = [...placed].reverse();
  host.appendChild(tableFor(ordered.slice(0, 8)));
  if (ordered.length > 8) {
    host.appendChild(
      el("details", { class: "more-list" }, [
        el("summary", { text: `View ${ordered.length - 8} earlier parent decisions` }),
        el("div", { class: "more-list-body" }, [tableFor(ordered.slice(8))]),
      ]),
    );
  }
}

/* ---------- lineage ---------- */

/* Every attempt is a node placed at the attempt it was made (x) and the value
 * it measured (y); the edge from its parent therefore rises when the child beat
 * the program it was derived from.  Attempts that never produced a score sit in
 * the n/a strip rather than being silently dropped. */
function renderLineage() {
  const host = document.getElementById("chart-lineage");
  clear(host);
  const { data } = state;
  const actions = allAttempts(data);
  const root = data.programs.find((item) => item.archive_seq === 0);
  const inheritedTargets = new Set(
    (data.prehistory?.branches || []).map((item) => item.target_action_id),
  );
  const warmStarts = data.programs.filter(
    (item) => item.warm_start && !inheritedTargets.has(item.warm_start.source_action_id),
  );
  if (!actions.length && !warmStarts.length) {
    host.appendChild(el("div", { class: "empty", text: "No attempts yet." }));
    return;
  }

  const rootKey = root ? root.commit_sha : "root";
  const nodeBySha = new Map();
  actions.forEach((action, i) => {
    const sha = action.evaluation.child_commit_sha;
    if (!sha) return;
    nodeBySha.set(sha, {
      sha,
      ordinal: i,
      action,
      value: action.evaluation.child_primary_value,
      admitted: displayAdmitted(action),
    });
  });

  const values = [...nodeBySha.values()].map((node) => node.value).filter((v) => v != null);
  if (root && root.primary_value != null) values.push(root.primary_value);
  for (const program of warmStarts) {
    if (program.primary_value != null) values.push(program.primary_value);
  }

  const W = host.clientWidth || 1100;
  const H = 342;
  const pad = { left: 96, right: 16, top: 42, bottom: 30 };
  const naY = H - pad.bottom - 10;
  const yTop = pad.top;
  const yBottom = naY - 30;
  let lo = values.length ? Math.min(...values) : 0;
  let hi = values.length ? Math.max(...values) : 1;
  const spread = hi - lo || (hi || 1) * 0.2 || 1;
  // a pass rate cannot exceed its own bounds, so headroom must not print 104%
  lo = Math.max(0, lo - spread * 0.06);
  hi = Math.min(1, hi + spread * 0.06);

  const canvas = svg("svg", {
    viewBox: `0 0 ${W} ${H}`,
    width: "100%",
    height: H,
  });
  const n = Math.max(actions.length, 1);
  const x = (i) => pad.left + (n === 1 ? 0.5 : i / (n - 1)) * (W - pad.left - pad.right);
  const y = (v) => yBottom - ((v - lo) / (hi - lo)) * (yBottom - yTop);
  const dense = n > 26;

  for (let t = 0; t <= 4; t += 1) {
    const v = lo + ((hi - lo) * t) / 4;
    canvas.appendChild(
      svg("line", { x1: pad.left - 24, x2: W - pad.right, y1: y(v), y2: y(v), stroke: "#f0efe9" }),
    );
    canvas.appendChild(
      svg("text", {
        x: pad.left - 30,
        y: y(v) + 3.5,
        text: metricValue(v),
        fill: "#a3a7b0",
        "font-size": 10,
        "text-anchor": "end",
      }),
    );
  }
  canvas.appendChild(
    svg("line", {
      x1: pad.left - 24,
      x2: W - pad.right,
      y1: naY,
      y2: naY,
      stroke: "#f0efe9",
      "stroke-dasharray": "3 3",
    }),
  );
  canvas.appendChild(
    svg("text", {
      x: pad.left - 30,
      y: naY + 3.5,
      text: "n/a",
      fill: "#c3c1ba",
      "font-size": 10,
      "text-anchor": "end",
    }),
  );
  generationOverlay(canvas, x, pad.top, H - pad.bottom, W - pad.right, { labels: false });

  const pos = new Map();
  pos.set(rootKey, {
    x: pad.left - 24,
    y: root && root.primary_value != null ? y(root.primary_value) : naY,
  });
  warmStarts.forEach((program, index) => {
    pos.set(program.commit_sha, {
      x: pad.left - 24 + (index + 1) * 10,
      y: program.primary_value != null ? y(program.primary_value) : naY,
    });
  });
  for (const node of nodeBySha.values()) {
    pos.set(node.sha, { x: x(node.ordinal), y: node.value == null ? naY : y(node.value) });
  }

  /* A curved edge separates the parent link from the gridlines and keeps
   * fan-outs from one popular parent readable. */
  const edge = (fromSha, toSha, opts) => {
    const p1 = pos.get(fromSha);
    const p2 = pos.get(toSha);
    if (!p1 || !p2) return;
    const mx = (p1.x + p2.x) / 2;
    canvas.appendChild(
      svg("path", {
        d: `M ${p1.x} ${p1.y} C ${mx} ${p1.y} ${mx} ${p2.y} ${p2.x} ${p2.y}`,
        fill: "none",
        stroke: opts.stroke,
        opacity: opts.opacity,
        "stroke-width": opts.width,
        "stroke-dasharray": opts.dashed ? "4 4" : null,
      }),
    );
  };

  for (const node of nodeBySha.values()) {
    const parentSha =
      node.action.parent_label === "Root" || node.action.parent_id === "root"
        ? rootKey
        : node.action.parent_commit_sha || node.action.parent_id;
    edge(parentSha, node.sha, {
      stroke: node.admitted ? getCss("--green") : "#c4c1b8",
      width: node.admitted ? 2 : 1.2,
      opacity: node.admitted ? 0.9 : 0.55,
      dashed: false,
    });
  }

  let bestSha = null;
  const minimize = data.meta.metric_direction === "minimize";
  let bestValue = minimize ? Infinity : -Infinity;
  for (const node of nodeBySha.values()) {
    if (node.value != null && (minimize ? node.value < bestValue : node.value > bestValue)) {
      bestValue = node.value;
      bestSha = node.sha;
    }
  }

  const drawNode = (key, node) => {
    const p = pos.get(key);
    if (!p) return;
    const isRoot = node === null;
    const isBest = key === bestSha;
    const color = isRoot
      ? "#3b3f47"
      : node.admitted
        ? getCss("--green")
        : getCss("--red");
    const r = isRoot ? 8 : dense ? 5.5 : 7;
    const group = svg("g", { style: isRoot ? "" : "cursor:pointer" });
    if (isBest) {
      group.appendChild(
        svg("circle", {
          cx: p.x,
          cy: p.y,
          r: r + 5,
          fill: "none",
          stroke: "#d4a017",
          "stroke-width": 1.6,
          "stroke-dasharray": "2.5 2.5",
        }),
      );
    }
    group.appendChild(
      svg("circle", { cx: p.x, cy: p.y, r, fill: "#fff", stroke: color, "stroke-width": 2.2 }),
    );
    const value = isRoot ? (root ? root.primary_value : null) : node.value;
    if (value != null) {
      group.appendChild(
        svg("circle", {
          cx: p.x,
          cy: p.y,
          r: r - 5 + 3.5 * Math.min(Math.max(value, 0), 1) + 1.5,
          fill: color,
        }),
      );
    }
    if (isRoot || !dense || isBest) {
      group.appendChild(
        svg("text", {
          x: p.x,
          y: p.y + r + 13,
          text: isRoot ? "root" : `#${attemptNumber(node.action)}`,
          "text-anchor": "middle",
          "font-size": 10,
          fill: "#6f7480",
        }),
      );
      group.appendChild(
        svg("text", {
          x: p.x,
          y: p.y - r - 6,
          text: (value != null ? metricValue(value) : "") + (isBest ? " ★" : ""),
          "text-anchor": "middle",
          "font-size": 9.5,
          fill: isBest ? "#b7791f" : "#a3a7b0",
          "font-weight": isBest ? "bold" : "normal",
        }),
      );
    }
    if (!isRoot) {
      group.addEventListener("click", () => openDrawer(node.action.action_id));
      group.addEventListener("mousemove", (event) =>
        showTip(event, esc(readable(node.action.direction.failure_mechanism)), [
          `Attempt #${attemptNumber(node.action)} — ${esc(node.action.prehistory?.source_run_id || node.action.batch_id.replace(/^batch-?/, "Batch "))}`,
          `score <b>${metricValue(node.value)}</b> — paired change ${points(node.action.evaluation.primary_delta_mean)}`,
          `${esc(archiveVerdict(node.action).label)} — ${esc(parentLabel(node.action))}`,
        ]),
      );
      group.addEventListener("mouseleave", hideTip);
    }
    canvas.appendChild(group);
  };

  drawNode(rootKey, null);
  for (const program of warmStarts) {
    const p = pos.get(program.commit_sha);
    if (!p) continue;
    const warm = program.warm_start;
    const group = svg("g", { style: "cursor:pointer" });
    group.appendChild(
      svg("rect", {
        x: p.x - 6,
        y: p.y - 6,
        width: 12,
        height: 12,
        rx: 2,
        fill: "#fff",
        stroke: "#8b78cf",
        "stroke-width": 2.2,
      }),
    );
    group.appendChild(
      svg("text", {
        x: p.x,
        y: p.y - 11,
        text: metricValue(program.primary_value),
        "text-anchor": "middle",
        "font-size": 9.5,
        fill: "#7563b1",
      }),
    );
    group.appendChild(
      svg("text", {
        x: p.x,
        y: p.y + 19,
        text: `warm #${program.archive_seq}`,
        "text-anchor": "middle",
        "font-size": 9.5,
        fill: "#7563b1",
      }),
    );
    group.addEventListener("mousemove", (event) =>
      showTip(event, esc(program.label), [
        `Inherited from <b>${esc(warm.source_run_id)}</b>`,
        `Source action ${esc(tail(warm.source_action_id))}`,
        "Click to open the immutable source trace.",
      ]),
    );
    group.addEventListener("mouseleave", hideTip);
    group.addEventListener("click", async () => {
      hideTip();
      await loadRun(warm.source_run_id);
      if (state.index.actionById.has(warm.source_action_id)) {
        openDrawer(warm.source_action_id);
      }
    });
    canvas.appendChild(group);
  }
  for (const node of nodeBySha.values()) drawNode(node.sha, node);

  if (dense) axisX(canvas, n, x, H - 2);
  host.appendChild(canvas);
}

/* ---------- behavior map ---------- */

/* Two challengers can score the same and still not be the same program: what
 * separates them is *which* cells they solve.  That pass/fail vector is the
 * run's only real behavior descriptor, so the map places each attempt by how
 * far its solved set has drifted from the root's (x) against what that drift
 * bought (y).  A column of dots at one x means the search kept rewriting the
 * same behavior; occupied bins are the niches the run actually reached. */
function renderMap() {
  const host = document.getElementById("chart-map");
  clear(host);
  const note = document.getElementById("map-note");
  const cellIds = state.data.meta.cell_ids;
  const root = state.data.programs.find((item) => item.archive_seq === 0);

  if (!root || !cellIds.length) {
    note.textContent = "";
    host.appendChild(
      el("div", { class: "empty", text: "No root cell results to compare against." }),
    );
    return;
  }

  const drift = (results) =>
    cellIds.reduce((total, cellId) => {
      const mine = results[cellId];
      const theirs = root.cell_results[cellId];
      if (mine === null || mine === undefined || theirs === null || theirs === undefined) {
        return total;
      }
      return total + (mine === theirs ? 0 : 1);
    }, 0);

  const points = [];
  state.data.actions.forEach((action, i) => {
    const value = action.evaluation.child_primary_value;
    if (value === null || value === undefined) return;
    if (!action.evaluation.completed_at) return;
    points.push({ action, i, x: drift(action.evaluation.cell_results), y: value });
  });
  if (!points.length) {
    note.textContent = "";
    host.appendChild(el("div", { class: "empty", text: "No scored attempts yet." }));
    return;
  }

  const W = host.clientWidth || 1100;
  const H = 340;
  const pad = { left: 84, right: 16, top: 22, bottom: 40 };
  const canvas = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H });

  // x counts cells, so it stays integral and anchored at "same as root"
  const xhi = Math.max(1, ...points.map((p) => p.x));
  const xlo = 0;
  const scores = points.map((p) => p.y).concat(root.primary_value ?? []);
  const span = Math.max(...scores) - Math.min(...scores) || 0.1;
  const ylo = Math.max(0, Math.min(...scores) - span * 0.12);
  const yhi = Math.min(1, Math.max(...scores) + span * 0.12);
  const x = (v) => pad.left + ((v - xlo) / (xhi - xlo)) * (W - pad.left - pad.right);
  const y = (v) => H - pad.bottom - ((v - ylo) / (yhi - ylo)) * (H - pad.top - pad.bottom);

  const NX = Math.min(14, xhi + 1);
  const NY = 8;
  const elite = new Map();
  let plo = Infinity;
  let phi = -Infinity;
  for (const point of points) {
    plo = Math.min(plo, point.y);
    phi = Math.max(phi, point.y);
    const bx = Math.min(NX - 1, Math.floor(((point.x - xlo) / (xhi - xlo)) * NX));
    const by = Math.min(NY - 1, Math.floor(((point.y - ylo) / (yhi - ylo)) * NY));
    const key = `${bx}:${by}`;
    const held = elite.get(key);
    if (held === undefined || point.y > held) elite.set(key, point.y);
  }
  const bw = (W - pad.left - pad.right) / NX;
  const bh = (H - pad.top - pad.bottom) / NY;
  for (const [key, value] of elite) {
    const [bx, by] = key.split(":").map(Number);
    const t = phi > plo ? (value - plo) / (phi - plo) : 1;
    canvas.appendChild(
      svg("rect", {
        x: pad.left + bx * bw,
        y: H - pad.bottom - (by + 1) * bh,
        width: bw,
        height: bh,
        fill: getCss("--green"),
        opacity: 0.04 + 0.16 * t,
        "pointer-events": "none",
      }),
    );
  }
  for (let i = 0; i <= NX; i += 1) {
    canvas.appendChild(
      svg("line", {
        x1: pad.left + i * bw,
        x2: pad.left + i * bw,
        y1: pad.top,
        y2: H - pad.bottom,
        stroke: "#f0efe9",
      }),
    );
  }
  for (let j = 0; j <= NY; j += 1) {
    canvas.appendChild(
      svg("line", {
        x1: pad.left,
        x2: W - pad.right,
        y1: pad.top + j * bh,
        y2: pad.top + j * bh,
        stroke: "#f0efe9",
      }),
    );
  }

  for (let t = 0; t <= 4; t += 1) {
    const vy = ylo + ((yhi - ylo) * t) / 4;
    canvas.appendChild(
      svg("text", {
        x: pad.left - 8,
        y: y(vy) + 3.5,
        text: metricValue(vy),
        "text-anchor": "end",
        "font-size": 10,
        fill: "#a3a7b0",
      }),
    );
    const vx = Math.round(xlo + ((xhi - xlo) * t) / 4);
    canvas.appendChild(
      svg("text", {
        x: x(vx),
        y: H - pad.bottom + 16,
        text: vx,
        "text-anchor": "middle",
        "font-size": 10,
        fill: "#a3a7b0",
      }),
    );
  }
  canvas.appendChild(
    svg("text", {
      x: W - pad.right,
      y: H - 6,
      text: `cells decided differently from root (of ${cellIds.length})`,
      "text-anchor": "end",
      "font-size": 10,
      fill: "#6f7480",
    }),
  );
  canvas.appendChild(
    svg("text", {
      x: pad.left - 8,
      y: 11,
      text: state.data.meta.metric_name || "primary",
      "text-anchor": "end",
      "font-size": 10,
      fill: "#6f7480",
    }),
  );

  // the root sits at x=0 by construction: it is the reference behavior
  if (root.primary_value !== null && root.primary_value !== undefined) {
    const ry = y(root.primary_value);
    canvas.appendChild(
      svg("line", {
        x1: pad.left,
        x2: W - pad.right,
        y1: ry,
        y2: ry,
        stroke: getCss("--muted"),
        "stroke-width": 1,
        "stroke-dasharray": "3 4",
        opacity: 0.5,
      }),
    );
    canvas.appendChild(
      svg("rect", {
        x: x(0) - 4,
        y: ry - 4,
        width: 8,
        height: 8,
        fill: "#fff",
        stroke: getCss("--muted"),
        "stroke-width": 1.6,
      }),
    );
    canvas.appendChild(
      svg("text", {
        x: x(0) + 9,
        y: ry - 7,
        text: "root",
        "font-size": 9.5,
        fill: "#6f7480",
      }),
    );
  }

  let best = null;
  for (const point of points) if (best === null || point.y > best.y) best = point;

  for (const point of points) {
    const cx = x(point.x);
    const cy = y(point.y);
    const color = point.action.evaluation.admitted ? getCss("--green") : getCss("--red");
    const group = svg("g", { style: "cursor:pointer" });
    if (point === best) {
      group.appendChild(
        svg("circle", {
          cx,
          cy,
          r: 11,
          fill: "none",
          stroke: "#d4a017",
          "stroke-width": 1.6,
          "stroke-dasharray": "2.5 2.5",
        }),
      );
    }
    group.appendChild(
      svg("circle", { cx, cy, r: 6.5, fill: "#fff", stroke: color, "stroke-width": 2.2 }),
    );
    group.appendChild(
      svg("circle", {
        cx,
        cy,
        r: 1.5 + 3.5 * (phi > plo ? (point.y - plo) / (phi - plo) : 1),
        fill: color,
      }),
    );
    group.addEventListener("click", () => openDrawer(point.action.action_id));
    group.addEventListener("mousemove", (event) =>
      showTip(event, `Attempt #${point.i} — ${esc(point.action.evaluation.admission_fate || "")}`, [
        esc(point.action.direction.failure_mechanism),
        `${point.x} of ${cellIds.length} cells decided differently from root`,
        `measured <b>${metricValue(point.y)}</b> — root ${metricValue(root.primary_value)}`,
      ]),
    );
    group.addEventListener("mouseleave", hideTip);
    canvas.appendChild(group);
  }

  host.appendChild(canvas);
  const reached = elite.size;
  note.textContent =
    `each dot = one scored challenger — shaded bins = reached niches (${reached}` +
    ` of ${NX * NY}), darker = better elite`;
}

/* ---------- attempts table ---------- */

function renderAttempts() {
  const host = document.getElementById("attempts");
  clear(host);
  const actions = allAttempts();
  if (!actions.length) {
    host.appendChild(el("div", { class: "empty", text: "No attempts yet." }));
    return;
  }
  const controls = el("div", { class: "filters" });
  const filters = [
    ["recent", "Recent 6"],
    ["active", "In progress"],
    ["stopped", "Stopped"],
    ["completed", "Completed"],
    ["all", "All"],
  ];
  for (const [key, label] of filters) {
    controls.appendChild(
      el("button", {
        class: `filter${state.attemptFilter === key ? " active" : ""}`,
        type: "button",
        text: label,
        onclick: () => {
          state.attemptFilter = key;
          renderAttempts();
        },
      }),
    );
  }
  let displayed;
  if (state.attemptFilter === "recent") displayed = actions.slice(-6).reverse();
  else if (state.attemptFilter === "active") displayed = actions.filter((item) => ["running", "evaluating"].includes(item.lifecycle_stage));
  else if (state.attemptFilter === "stopped") displayed = actions.filter((item) => ["blocked", "aborted"].includes(item.lifecycle_stage));
  else if (state.attemptFilter === "completed") displayed = actions.filter((item) => item.lifecycle_stage === "completed");
  else displayed = actions;
  controls.appendChild(
    el("span", { class: "attempt-count", text: `Showing ${displayed.length} of ${actions.length}` }),
  );
  host.appendChild(controls);

  const table = el("table", { class: "attempts" });
  table.innerHTML =
    `<thead><tr>
      <th>Attempt</th><th>Direction</th><th>Status</th>
      <th style="text-align:right">${esc(metricLabel(state.data.meta.metric_name))}</th>
      <th style="text-align:right">Paired change</th><th>Archive</th><th>Measured cells</th>
    </tr></thead>`;
  const body = el("tbody");

  let currentBatch = null;
  displayed.forEach((action) => {
    const evaluation = action.evaluation;
    const delta = evaluation.primary_delta_mean;
    const favorable =
      delta !== null &&
      (state.data.meta.metric_direction === "minimize" ? delta <= 0 : delta >= 0);
    const row = el("tr", {
      class: `att${action.action_id === state.selected ? " sel" : ""}`,
      onclick: () => openDrawer(action.action_id),
    });
    const cells = Object.values(evaluation.cell_results || {}).filter((value) => value !== null && value !== undefined);
    const passed = cells.filter(Boolean).length;
    const strip = Object.keys(evaluation.cell_results || {})
      .map((cellId) => {
        const value = evaluation.cell_results[cellId];
        const stateLabel = value === true ? "pass" : value === false ? "fail" : "not measured";
        return `<i class="${value === true ? "pass" : value === false ? "fail" : ""}" title="${esc(cellLabel(cellId))} — ${stateLabel}"></i>`;
      })
      .join("");
    const batchKey = `${action.prehistory?.source_run_id || state.data.meta.run_id}:${action.batch_id}`;
    if (batchKey !== currentBatch) {
      currentBatch = batchKey;
      const batch = state.data.batches.find((item) => item.batch_id === action.batch_id);
      const batchActions = actions.filter((item) =>
        item.batch_id === action.batch_id &&
        (item.prehistory?.source_run_id || state.data.meta.run_id) ===
          (action.prehistory?.source_run_id || state.data.meta.run_id)
      );
      const addedCount = batchActions.filter(displayAdmitted).length;
      const activeCount = batchActions.filter((item) => ["running", "evaluating"].includes(item.lifecycle_stage)).length;
      const blockedCount = batchActions.filter((item) => item.lifecycle_stage === "blocked").length;
      const abortedCount = batchActions.filter((item) => item.lifecycle_stage === "aborted").length;
      const completedCount = batchActions.filter((item) => item.lifecycle_stage === "completed").length;
      const curveIndex = state.data.curve.findIndex((item) => item.batch_id === action.batch_id);
      const beforeBest = curveIndex > 0 ? state.data.curve[curveIndex - 1].best_primary_value : null;
      const afterBest = curveIndex >= 0 ? state.data.curve[curveIndex].best_primary_value : null;
      const bestText = state.data.research
        ? 'B results shown per implementation'
        : action.prehistory
        ? `source trace from ${action.prehistory.source_run_id}`
        :
        completedCount === 0
          ? "not measured yet"
          : beforeBest !== null && afterBest !== null && afterBest !== beforeBest
          ? `best ${metricValue(beforeBest)} → ${metricValue(afterBest)}`
          : "best unchanged";
      const group = el("tr", { class: "batch-group" });
      const groupLabel = action.prehistory
        ? `${action.prehistory.source_run_id} — ${action.batch_id}`
        : action.batch_id.replace(/^batch-?/, "Batch ");
      group.innerHTML = `<td colspan="7">${esc(groupLabel)}` +
        `<span class="batch-group-detail">${batchActions.length} planned — ${activeCount} active — ${completedCount} completed` +
        `${abortedCount ? ` — ${abortedCount} stopped` : ""}` +
        `${blockedCount ? ` — ${blockedCount} blocked` : ""} — ${addedCount} added — ${bestText}` +
        `${batch ? ` — $${(batch.cost_usd || 0).toFixed(2)}` : ""}</span></td>`;
      body.appendChild(group);
    }
    const target = directionScope(action.direction)[0];
    const context = [
      parentLabel(action),
      target ? scopeLabel(target) : null,
    ].filter(Boolean).join(" — ");
    const archive = archiveVerdict(action);
    const externalFailure = action.external_execution?.failure_code;
    const statusDetail = externalFailure
      ? `<div class="fam">${esc(humanize(externalFailure))}</div>`
      : action.effectiveness_label
        ? `<div class="fam">${esc(humanize(action.effectiveness_label))}</div>`
        : "";
    row.innerHTML = `
      <td class="idx">#${attemptNumber(action)}</td>
      <td class="hyp">${esc(directionHeadline(action.direction.failure_mechanism))}
        <div class="fam">${esc(context)}</div></td>
      <td><span class="badge ${lifecycleClass(action)}">${esc(lifecycleLabel(action))}</span>${statusDetail}</td>
      <td class="num" style="text-align:right">${metricValue(evaluation.child_primary_value)}</td>
      <td class="num ${delta === null ? "" : favorable ? "delta-pos" : "delta-neg"}" style="text-align:right">${
        points(delta)
      }</td>
      <td><span class="badge ${archive.className}">${archive.label}</span></td>
      <td class="num">${cells.length ? `${passed}/${cells.length} pass` : "—"}<div><span class="cellstrip" aria-label="${passed} of ${cells.length} measured cells passed">${strip}</span></div></td>`;
    body.appendChild(row);
  });
  table.appendChild(body);
  host.appendChild(table);
}

/* ---------- drawer ---------- */

const overlay = document.getElementById("overlay");
const drawer = document.getElementById("drawer");

function openDrawer(actionId) {
  state.selected = actionId;
  state.drawerTab = "summary";
  overlay.classList.add("show");
  drawer.classList.add("show");
  renderDrawer();
  renderAttempts();
}

function closeDrawer() {
  overlay.classList.remove("show");
  drawer.classList.remove("show");
  state.selected = null;
  renderAttempts();
}

function parentLabel(action) {
  if (action.parent_label === "Root" || action.parent_id === "root") {
    return "Parent: starting program";
  }
  const program = state.index.programByParentId.get(action.parent_id);
  const source = program?.source_action_id
    ? state.index.actionById.get(program.source_action_id)
    : state.index.actionByCommit.get(action.parent_commit_sha);
  return source
    ? `Parent: Attempt #${attemptNumber(source)}, ${readable(source.direction.failure_mechanism)}`
    : action.parent_label && action.parent_label !== "Unarchived parent"
      ? `Parent: ${action.parent_label}`
      : "Parent: archived program (identity unavailable)";
}

function armLabel(armId) {
  const action = state.data.actions.find((item) => item.arm_id === armId);
  return action ? readable(action.direction.failure_mechanism) : "Known mechanism";
}

function parentOptionLabel(option) {
  if (option.label === "Root" || option.archive_seq === 0 || option.parent_id === "root") {
    return "Root baseline — original starting program";
  }
  const source = state.index.actionByCommit.get(option.commit_sha);
  if (!source) return "Archived program";
  return `Attempt #${attemptNumber(source)}: ${readable(source.direction.failure_mechanism)}`;
}

function elapsed(start, end) {
  if (!start || !end) return null;
  const seconds = Math.max(0, (new Date(end) - new Date(start)) / 1000);
  if (seconds < 60) return `${seconds.toFixed(0)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

function renderDrawer() {
  const host = document.getElementById("drawer-body");
  clear(host);
  const action = state.index.actionById.get(state.selected);
  if (!action) return;

  host.appendChild(
    el("div", {
      html:
        `<div class="kicker">Attempt #${attemptNumber(action)}</div>` +
        `<h1 style="font-size:26px">${esc(directionHeadline(action.direction.failure_mechanism))}</h1>` +
        `<div class="subtitle">${esc(action.batch_id.replace(/^batch-?/, "Batch "))} — ${esc(parentLabel(action))}</div>`,
    }),
  );

  const decision = state.index.decisionByAction.get(action.action_id);
  const tabs = [
    ["summary", "Overview", null],
    ["diff", "Change", null],
    ["evaluation", "Evidence", Object.keys(action.evaluation.cell_results || {}).length],
    ["decision", action.research ? "Strategy decision" : "Decision trace D1 / D2 / D3", decision ? decision.direction_options.length : 0],
  ];
  const tabRow = el("div", { class: "tabs" });
  for (const [key, label, count] of tabs) {
    tabRow.appendChild(
      el("div", {
        class: `tab${state.drawerTab === key ? " active" : ""}`,
        html: `${label}${count ? `<span class="cnt">${count}</span>` : ""}`,
        onclick: () => {
          state.drawerTab = key;
          renderDrawer();
        },
      }),
    );
  }
  host.appendChild(tabRow);

  const panel = el("div");
  if (action.research && state.drawerTab !== 'diff') drawResearchDetail(panel, action);
  else if (state.drawerTab === "summary") drawSummary(panel, action);
  else if (state.drawerTab === "decision") drawDecision(panel, action, decision);
  else if (state.drawerTab === "diff") drawDiff(panel, action);
  else drawEvaluation(panel, action);
  host.appendChild(panel);
}

function drawSummary(host, action) {
  const evaluation = action.evaluation;
  const execution = action.execution;
  const proposer = action.proposer || {};
  const worker = action.worker || {};
  const added = displayAdmitted(action);
  const finished = evaluation.completed_at !== null;
  const archive = archiveVerdict(action);
  const stage = lifecycleLabel(action);
  const outcomeLead = action.prehistory?.warm_start_target
    ? `Accepted into this run's warm-start pool (source decision: ${humanize(evaluation.admission_fate || "not recorded")})`
    : action.lifecycle_stage === "blocked"
    ? "Implementation blocked"
    : action.lifecycle_stage === "aborted"
      ? "Experiment stopped before measurement"
    : !finished
      ? `${stage}: archive decision pending`
      : evaluation.admitted === null
        ? evaluation.admission_fate === "admitted"
          ? "Admission passed; archive update pending"
          : `${stage}: archive decision pending`
        : added
          ? "Added to the archive"
          : "Not added to the archive";
  host.appendChild(
    el("div", { class: "outcome-banner" }, [
      el("div", {}, [
        el("div", {
          class: "lead",
          text: outcomeLead,
        }),
        el("div", {
          class: "detail",
          text: action.external_execution?.failure_code
            ? humanize(action.external_execution.failure_code)
            : finished
              ? `Paired change ${points(evaluation.primary_delta_mean)} — policy feedback ${action.effectiveness_label || "not recorded"}`
              : "This planned direction has not produced a B-tier measurement yet.",
        }),
      ]),
      el("div", { class: "score", text: metricValue(evaluation.child_primary_value) }),
    ]),
  );

  host.appendChild(el("h3", { text: "Proposed" }));
  const proposerIdentity = [proposer.provider, proposer.model].filter(Boolean).join(" — ");
  host.appendChild(
    el("div", { class: "role-line" }, [
      el("span", { html: `Proposer <strong>${esc(proposerIdentity || "identity not recorded")}</strong>` }),
      proposer.candidate_count
        ? el("span", { text: `${proposer.candidate_count} candidates generated` })
        : null,
      proposer.repair_feedback ? el("span", { text: "repaired proposal" }) : null,
    ]),
  );
  host.appendChild(el("div", { class: "prose", text: readable(action.direction.failure_mechanism) }));
  host.appendChild(el("div", { class: "mechanism-title", text: readable(action.direction.implementation_direction) }));
  const proposed = el("div", { class: "overview-grid", style: { marginTop: "12px" } });
  const scopes = directionScope(action.direction).map(scopeLabel);
  for (const [key, value] of [
    ["edit scope", scopes.join("; ") || "not recorded"],
    ["resolved scope", (action.direction.resolved_scope || []).join(", ") || "not recorded"],
    ["evidence", action.direction.evidence_refs.map(cellLabel).join(", ") || "none cited"],
  ]) {
    proposed.appendChild(el("div", { class: "overview-fact" }, [
      el("span", { class: "k", text: key }),
      el("span", { class: "v", text: value }),
    ]));
  }
  host.appendChild(proposed);
  const citedBriefs = action.direction.evidence_refs
    .map((cellId) => state.index.cellBriefById.get(cellId))
    .filter(Boolean);
  if (citedBriefs.length) {
    host.appendChild(el("h3", { text: "Visible evidence cited by the proposer" }));
    for (const brief of citedBriefs) {
      host.appendChild(
        el("details", { class: "evidence-brief" }, [
          el("summary", { text: brief.title || cellLabel(brief.cell_id) }),
          el("div", { class: "prose", text: brief.statement }),
          brief.observed_failure
            ? hint(`Observed failure: ${humanize(brief.observed_failure)}`)
            : null,
        ]),
      );
    }
  }

  host.appendChild(el("h3", { text: "Implemented" }));
  const configuredWorker = state.data.meta.worker || {};
  const workerIdentity = [
    worker.provider || configuredWorker.provider,
    worker.model || configuredWorker.model,
  ].filter(Boolean).join(" — ") ||
    (worker.runner_id ? `runner ${humanize(worker.runner_id)} — model not recorded` : "identity not recorded");
  host.appendChild(el("div", { class: "role-line" }, [
    el("span", { html: `Worker <strong>${esc(workerIdentity)}</strong>` }),
  ]));
  const implementation = el("div", { class: "overview-grid" });
  const touched = execution.touched_files;
  const duration = execution.duration_seconds !== null && execution.duration_seconds !== undefined
    ? `${fmt(execution.duration_seconds, 1)}s`
    : elapsed(execution.started_at, execution.completed_at);
  const implementationFacts = [
    ["status", execution.worker_status || stage],
    ["adherence", execution.adherence || "not recorded"],
    ["files", touched.join(", ") || "not recorded"],
    ["duration", duration || "not recorded"],
    ["cost", typeof execution.cost_usd === "number" ? `$${execution.cost_usd.toFixed(3)}` : "not recorded"],
    ["commit", execution.result_commit_sha ? tail(execution.result_commit_sha) : "no commit"],
  ];
  if (execution.rework_count !== null && execution.rework_count !== undefined) {
    implementationFacts.push(["reworks", execution.rework_count]);
  }
  if (execution.harness_attempt_count !== null && execution.harness_attempt_count !== undefined) {
    implementationFacts.push(["harness attempts", execution.harness_attempt_count]);
  }
  if (execution.diagnostic_ran) {
    implementationFacts.push(["diagnostic", execution.diagnostic_passed ? "passed" : "failed"]);
  }
  for (const [key, value] of implementationFacts) {
    implementation.appendChild(el("div", { class: "overview-fact" }, [
      el("span", { class: "k", text: key }),
      el("span", { class: "v", text: value }),
    ]));
  }
  host.appendChild(implementation);
  const commitOverview = el("div", { class: "commit-overview", text: "Loading commit summary…" });
  host.appendChild(commitOverview);
  hydrateCommitOverview(commitOverview, action);

  if (execution.harness_attempts?.length) {
    const attempts = el("details", { class: "technical" });
    attempts.appendChild(
      el("summary", { text: `Worker attempt details (${execution.harness_attempts.length})` }),
    );
    const list = el("div", { class: "ranks", style: { marginTop: "10px" } });
    execution.harness_attempts.forEach((item, index) => {
      const report = item.report && typeof item.report === "object" ? item.report : {};
      const summary = report.summary || item.reason || item.exit_reason || item.status || "no summary recorded";
      list.appendChild(
        el("div", { class: "rank" }, [
          el("div", { class: "line" }, [
            el("div", { class: "name", text: `Worker attempt ${index + 1}` }),
            el("div", { class: "sub", text: String(summary) }),
          ]),
        ]),
      );
    });
    attempts.appendChild(list);
    host.appendChild(attempts);
  }

  host.appendChild(el("h3", { text: "Measured" }));
  const cells = Object.values(evaluation.cell_results || {}).filter((value) => typeof value === "boolean");
  const passed = cells.filter(Boolean).length;
  const measured = el("div", { class: "overview-grid" });
  for (const [key, value] of [
    ["score", metricValue(evaluation.child_primary_value)],
    ["paired change", points(evaluation.primary_delta_mean, 2)],
    ["archive", evaluation.admitted === null ? archive.label : action.archive_label || archive.label],
    ["policy feedback", action.effectiveness_label || "not recorded"],
    ["measured cells", cells.length ? `${passed}/${cells.length} pass` : "not recorded"],
    ["constraints", evaluation.constraints_pass === null ? "not recorded" : evaluation.constraints_pass ? "pass" : "fail"],
  ]) {
    measured.appendChild(el("div", { class: "overview-fact" }, [
      el("span", { class: "k", text: key }),
      el("span", { class: "v", text: value }),
    ]));
  }
  host.appendChild(measured);

  const technical = el("details", { class: "technical" });
  technical.appendChild(el("summary", { text: "Technical identifiers and diagnostics" }));
  const facts = el("div", { class: "kv", style: { marginTop: "12px" } });
  for (const [key, value] of [
    ["action id", action.action_id],
    ["parent commit", action.parent_commit_sha],
    ["direction id", action.direction.direction_id],
    ["proposal id", proposer.proposal_id || "not recorded"],
    ["proposal request", proposer.request_id || "not recorded"],
    ["slot", action.slot_kind || "not used by this carrier"],
    ["semantic match", action.match_kind || "not used by this carrier"],
    ["cosine distance", action.match_distance === null ? "not used" : fmt(action.match_distance, 4)],
    ["arm id", action.arm_id || "not used by this carrier"],
    ["evidence refs", action.direction.evidence_refs.join(", ") || "none"],
    ["not persisted", (state.data.meta.unavailable_fields || []).join(", ") || "none"],
  ]) {
    facts.appendChild(el("span", { class: "k", text: key }));
    facts.appendChild(el("span", { class: "v", text: value }));
  }
  if (Object.keys(execution.diagnostics || {}).length) {
    facts.appendChild(el("span", { class: "k", text: "diagnostics" }));
    facts.appendChild(el("span", { class: "v", text: JSON.stringify(execution.diagnostics) }));
  }
  technical.appendChild(facts);
  host.appendChild(technical);
}

async function hydrateCommitOverview(host, action) {
  const sha = action.execution.result_commit_sha || action.evaluation.child_commit_sha;
  if (!sha) {
    host.textContent = "This attempt produced no commit.";
    return;
  }
  let payload = state.diffCache.get(sha);
  try {
    if (!payload) {
      const response = await fetch(`/api/diff?sha=${encodeURIComponent(sha)}&run=${encodeURIComponent(state.runId)}`);
      payload = await response.json();
      state.diffCache.set(sha, payload);
    }
  } catch (error) {
    host.textContent = `Commit summary unavailable: ${error.message || error}`;
    return;
  }
  if (state.selected !== action.action_id || state.drawerTab !== "summary") return;
  if (payload.unavailable || payload.error) {
    host.textContent = payload.unavailable || payload.error;
    return;
  }
  const files = parseUnifiedDiff(payload.text || "");
  const added = files.reduce((sum, file) => sum + file.additions, 0);
  const removed = files.reduce((sum, file) => sum + file.deletions, 0);
  const folders = [...new Set(files.map((file) => {
    const split = file.path.lastIndexOf("/");
    return split >= 0 ? file.path.slice(0, split + 1) : "repository root";
  }))];
  clear(host);
  host.appendChild(
    el("div", {
      html: `<strong>${esc(readable(payload.subject || `Commit ${tail(sha)}`))}</strong> — ` +
        `${files.length} file${files.length === 1 ? "" : "s"} — ` +
        `<span class="delta-pos">+${added}</span> <span class="delta-neg">−${removed}</span>`,
    }),
  );
  if (folders.length) {
    host.appendChild(el("div", { text: `Folders: ${folders.join(", ")}`, style: { marginTop: "3px" } }));
  }
}

/* ---------- diff ---------- */

/* What the candidate actually wrote.  The direction is an intention and the
 * statistic is a verdict; only the patch says which edit produced them. */
async function drawDiff(host, action) {
  const sha = action.execution.result_commit_sha || action.evaluation.child_commit_sha;
  host.appendChild(
    el("div", {
      class: "hint",
      text: sha ? `commit ${sha}` : "this attempt never produced a commit",
    }),
  );
  if (!sha) return;
  const body = el("div", { class: "empty", text: "loading patch…" });
  host.appendChild(body);

  let payload = state.diffCache.get(sha);
  if (!payload) {
    try {
      const response = await fetch(`/api/diff?sha=${encodeURIComponent(sha)}&run=${encodeURIComponent(state.runId)}`);
      payload = await response.json();
      state.diffCache.set(sha, payload);
    } catch (error) {
      body.textContent = String(error.message || error);
      return;
    }
  }
  if (state.drawerTab !== "diff") return;
  if (payload.unavailable || payload.error) {
    body.textContent = payload.unavailable || payload.error;
    return;
  }

  const files = parseUnifiedDiff(payload.text || "");
  if (!files.length) {
    clear(host);
    host.appendChild(el("div", { class: "hint", text: "No text hunks were available; showing the raw Git summary." }));
    host.appendChild(el("pre", { class: "prose mono", text: payload.text || "the commit contains no displayable patch" }));
    return;
  }
  clear(host);
  const added = files.reduce((sum, file) => sum + file.additions, 0);
  const removed = files.reduce((sum, file) => sum + file.deletions, 0);
  host.appendChild(
    el("div", {
      class: "diff-summary",
      html:
        (payload.truncated ? "patch truncated by the server — " : "") +
        `${files.length} file${files.length > 1 ? "s" : ""} changed — ` +
        `<span class="delta-pos">+${added}</span> ` +
        `<span class="delta-neg">−${removed}</span> — commit <code>${esc(tail(sha))}</code>`,
    }),
  );
  const folders = new Map();
  for (const file of files) {
    const split = file.path.lastIndexOf("/");
    const folder = split >= 0 ? file.path.slice(0, split + 1) : "repository root";
    if (!folders.has(folder)) folders.set(folder, []);
    folders.get(folder).push({ ...file, displayPath: split >= 0 ? file.path.slice(split + 1) : file.path });
  }
  for (const [folder, folderFiles] of folders) {
    const folderAdded = folderFiles.reduce((sum, file) => sum + file.additions, 0);
    const folderRemoved = folderFiles.reduce((sum, file) => sum + file.deletions, 0);
    const group = el("section", { class: "diff-folder" });
    group.appendChild(
      el("div", { class: "diff-folder-head" }, [
        el("span", { class: "diff-folder-path", text: folder }),
        el("span", {
          class: "diff-folder-stat",
          text: `${folderFiles.length} file${folderFiles.length === 1 ? "" : "s"} — +${folderAdded} −${folderRemoved}`,
        }),
      ]),
    );
    for (const file of folderFiles) group.appendChild(renderDiffFile(file));
    host.appendChild(group);
  }
}

function parseUnifiedDiff(text) {
  const files = [];
  let file = null;
  let oldLine = 0;
  let newLine = 0;
  for (const line of text.split("\n")) {
    const header = line.match(/^diff --git a\/(.+?) b\/(.+)$/);
    if (header) {
      file = {
        oldPath: header[1],
        path: header[2],
        meta: [],
        rows: [],
        additions: 0,
        deletions: 0,
      };
      files.push(file);
      continue;
    }
    if (!file) continue; // commit header and --stat block precede the first file
    if (line.startsWith("@@")) {
      const at = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (at) {
        oldLine = Number(at[1]);
        newLine = Number(at[2]);
      }
      file.rows.push({ type: "hunk", text: line });
      continue;
    }
    if (
      /^(\+\+\+|---|index |new file|deleted file|similarity|rename |Binary files|old mode|new mode)/.test(
        line,
      )
    ) {
      file.meta.push(line);
      continue;
    }
    if (!file.rows.length) continue; // stray text outside any hunk
    if (line === "\\ No newline at end of file") {
      file.rows.push({ type: "marker", text: line });
    } else if (line.startsWith("+")) {
      file.rows.push({ type: "add", new: newLine, text: line.slice(1) });
      newLine += 1;
      file.additions += 1;
    } else if (line.startsWith("-")) {
      file.rows.push({ type: "del", old: oldLine, text: line.slice(1) });
      oldLine += 1;
      file.deletions += 1;
    } else {
      file.rows.push({ type: "ctx", old: oldLine, new: newLine, text: line.slice(1) });
      oldLine += 1;
      newLine += 1;
    }
  }
  return files;
}

function renderDiffFile(file) {
  const status = file.meta.some((line) => line.startsWith("new file"))
    ? " — new"
    : file.meta.some((line) => line.startsWith("deleted file"))
      ? " — deleted"
      : file.oldPath && file.oldPath !== file.path
        ? ` — renamed from ${file.oldPath}`
        : "";
  const rows = file.rows
    .map((row) =>
      row.type === "hunk"
        ? `<tr class="hunk"><td class="ln"></td><td class="ln"></td><td class="code">${esc(row.text)}</td></tr>`
        : `<tr class="${row.type}"><td class="ln">${row.old ?? ""}</td>` +
          `<td class="ln">${row.new ?? ""}</td>` +
          `<td class="code">${esc(row.text) || " "}</td></tr>`,
    )
    .join("");
  return el("div", {
    class: "dfile",
    html:
      `<div class="dfile-head"><span>${esc(file.displayPath || file.path)}${esc(status)}</span>` +
      `<span class="stat"><span class="plus">+${file.additions}</span>` +
      `<span class="minus">−${file.deletions}</span></span></div>` +
      `<table class="dtable">${rows}</table>`,
  });
}

function hint(text) {
  return el("div", { class: "hint", text });
}

function rank({ fraction, name, sub, pick, pickLabel = "chosen", out, disc }) {
  const row = el("div", {
    class: `rank${pick ? " pick" : ""}${out ? " out" : ""}${disc ? " disc" : ""}`,
  });
  if (fraction > 0) {
    row.appendChild(
      el("div", { class: "fill", style: { width: `${Math.max(2, fraction * 100)}%` } }),
    );
  }
  row.appendChild(
    el("div", { class: "line" }, [
      el("div", { class: "name", text: name, title: name }),
      pick ? el("span", { class: "tag existing", text: pickLabel }) : null,
      el("div", { class: "sub", text: sub }),
    ]),
  );
  return row;
}

/* Long slates taper into candidates nobody needs to read. */
function trimmed(options, render, keep = 6) {
  const at = (option) => render(option, options.indexOf(option));
  if (options.length <= keep + 1) return options.map(at);
  const rest = options.slice(keep);
  const chosen = rest.filter((option) => option.selected);
  const rows = options.slice(0, keep).concat(chosen).map(at);
  rows.push(
    el("div", {
      class: "sub",
      style: { cursor: "pointer", padding: "4px 2px", color: "var(--accent)", fontSize: "12px" },
      text: `+ ${rest.length - chosen.length} lower-ranked candidates`,
      onclick: (event) => {
        const anchor = event.currentTarget;
        for (const option of rest) {
          if (option.selected) continue;
          anchor.parentNode.insertBefore(at(option), anchor);
        }
        anchor.remove();
      },
    }),
  );
  return rows;
}

function drawDecision(host, action, decision) {
  if (!decision) {
    host.appendChild(el("div", { class: "empty", text: "No decision record for this attempt." }));
    return;
  }

  const memoryBatch = state.data.batches.find((batch) => batch.batch_id === action.batch_id);
  const d2 = action.decision_trace?.d2 || {};
  const failureCards = Array.isArray(d2.failure_history?.cards)
    ? d2.failure_history.cards
    : [];
  const d2DirectionLabels = new Map();
  host.appendChild(el("h3", { text: "D1 Choose a parent program" }));
  const parents = decision.parent_options.filter((option) => option.eligible);
  const selectedParent = parents.find((option) => option.selected);
  const parentScale = scaler(parents.map((option) => option.index));
  host.appendChild(
    hint(
      `${selectedParent ? parentOptionLabel(selectedParent) : "No selected parent was recorded"}. ` +
        `${parents.length} eligible programs were considered. ` +
        (parentScale.tied
          ? "Eligible parents had equal policy scores, so the recorded tie break selected this parent."
          : decision.method === "ucb"
            ? "The bar shows the UCB parent selection index."
            : "The bar shows the deterministic Textual rank order."),
    ),
  );
  const parentList = el("div", { class: "ranks" });
  for (const row of trimmed(parents, (option) =>
    rank({
      fraction: parentScale.of(option.index),
      pick: option.selected,
      out: !option.eligible,
      name:
        parentOptionLabel(option),
      sub:
        option.primary_value === null ? "archive score not recorded" : `archive score ${metricValue(option.primary_value)}`,
    }),
  )) {
    parentList.appendChild(row);
  }
  host.appendChild(parentList);
  const technicalD1 = el("details", { class: "technical" }, [
    el("summary", { text: "Complete D1 technical record" }),
  ]);
  technicalD1.appendChild(
    el("div", {
      class: "prose mono",
      text: JSON.stringify(action.decision_trace?.d1 || {}, null, 2),
    }),
  );
  host.appendChild(technicalD1);

  if (decision.slots.length) {
    host.appendChild(el("h3", { text: "D2 Allocate capacity and generate proposals" }));
    const allocation = decision.allocation || {};
    host.appendChild(
      hint(
        allocation.cold_start
          ? "cold start: every slot explores a new mechanism"
          : `${allocation.generation_capacity ?? decision.slots.length} slots, ${allocation.discover_quota ?? 0} discovery quota`,
      ),
    );
    const slotScale = scaler(decision.slots.map((option) => option.index));
    const slotList = el("div", { class: "ranks" });
    for (const slot of decision.slots) {
      slotList.appendChild(
        rank({
          fraction: slotScale.of(slot.index),
          disc: slot.kind === "discover",
          name: slot.kind === "discover"
            ? `Discovery slot ${slot.ordinal + 1}: generate a new mechanism`
            : armLabel(slot.source_arm_id),
          sub:
            slot.kind === "discover"
              ? "opens a new mechanism"
              : "another realization of a known mechanism",
        }),
      );
    }
    host.appendChild(slotList);

    const generationAttempts = Array.isArray(d2.generation_attempts)
      ? d2.generation_attempts
      : [];
    host.appendChild(el("h4", { text: `D2 proposal generation (${generationAttempts.length} attempts)` }));
    if (!generationAttempts.length) {
      host.appendChild(hint("No proposal-generation attempts were recorded for this allocation."));
    } else {
      const attemptList = el("div", { class: "ranks" });
      generationAttempts.forEach((attempt, index) => {
        const attemptLabel = `D2 attempt ${index + 1}`;
        if (attempt.direction_id) d2DirectionLabels.set(attempt.direction_id, attemptLabel);
        const directionOption = decision.direction_options.find(
          (option) => option.direction.direction_id === attempt.direction_id,
        );
        const slot = decision.slots.find((item) => item.slot_id === attempt.source_slot_id);
        const adherent = attempt.status === "adherent";
        const armMechanism = attempt.arm_spec?.failure_mechanism
          || directionOption?.direction?.failure_mechanism;
        const slotLabel = slot?.kind === "discover"
          ? `discovery slot ${(slot.ordinal ?? 0) + 1}`
          : slot
            ? `existing-arm slot ${(slot.ordinal ?? 0) + 1}`
            : "unresolved slot";
        const parts = [
          slotLabel,
          humanize(attempt.status || "status not recorded"),
          attempt.match_kind ? `semantic match ${humanize(attempt.match_kind)}` : null,
          attempt.repair_feedback ? "regenerated after feedback" : null,
          attempt.reason ? readable(attempt.reason) : null,
          [attempt.provider, attempt.model].filter(Boolean).join(" / ") || null,
        ].filter(Boolean);
        attemptList.appendChild(
          rank({
            fraction: directionOption ? 1 : 0,
            pick: Boolean(directionOption),
            pickLabel: "entered this D3",
            out: !adherent,
            disc: slot?.kind === "discover",
            name: armMechanism
              ? `${attemptLabel} D2 arm: ${readable(armMechanism)}`
              : `${attemptLabel} — ${humanize(attempt.status || "not accepted")}`,
            sub: parts.join(" — "),
          }),
        );
      });
      host.appendChild(attemptList);
    }
  } else {
    host.appendChild(el("h3", { text: "D2 Build context and generate proposals" }));
    const memoryRules = parseMemoryRules(memoryBatch?.memory_content || "");
    const fixedEvidence = Array.isArray(d2.fixed_evidence) ? d2.fixed_evidence : [];
    const proposals = Array.isArray(d2.proposals) ? d2.proposals : [];
    const proposalCount = proposals.length || decision.direction_options.length;
    const contextParts = [
      memoryRules.length ? `${memoryRules.length} saved Policy Memory rules` : "no saved Policy Memory rules",
      failureCards.length ? `${failureCards.length} related failure records` : "no prior failure records",
      fixedEvidence.length ? `${fixedEvidence.length} fixed evidence items` : `${action.direction.evidence_refs.length} cited visible cases`,
      `${proposalCount} candidate directions generated`,
    ];
    host.appendChild(
      hint(
        `This is the frozen context used for this exact proposal: ${contextParts.join("; ")}.`,
      ),
    );
    if (memoryRules.length) {
      const context = el("div", { class: "memory-rules" });
      for (const rule of memoryRules) {
        context.appendChild(
          el("article", { class: "memory-rule" }, [
            el("h4", { text: rule.failure }),
            rule.response ? el("p", { text: `Recommended change: ${rule.response}` }) : null,
          ]),
        );
      }
      host.appendChild(context);
    }

    host.appendChild(el("h4", { text: `D2 generated proposals (${proposals.length})` }));
    if (!proposals.length) {
      host.appendChild(hint("No individual proposal records were persisted for this Textual decision."));
    } else {
      const proposalList = el("div", { class: "ranks" });
      proposals.forEach((proposal, index) => {
        const proposalLabel = `D2 proposal ${index + 1}`;
        if (proposal.direction_id) d2DirectionLabels.set(proposal.direction_id, proposalLabel);
        const directionOption = decision.direction_options.find(
          (option) => option.direction.direction_id === proposal.direction_id,
        );
        const metadata = proposal.metadata || {};
        const parts = [
          [proposal.provider, proposal.model].filter(Boolean).join(" / ") || null,
          Number.isFinite(metadata.input_tokens) ? `${metadata.input_tokens} input tokens` : null,
          Number.isFinite(metadata.output_tokens) ? `${metadata.output_tokens} output tokens` : null,
          Number.isFinite(metadata.cost_usd) ? `$${metadata.cost_usd.toFixed(4)}` : null,
        ].filter(Boolean);
        proposalList.appendChild(
          rank({
            fraction: directionOption ? 1 : 0,
            pick: Boolean(directionOption),
            pickLabel: "entered D3",
            out: !directionOption,
            name: directionOption
              ? `${proposalLabel} D2 direction: ${readable(directionOption.direction.failure_mechanism)}`
              : `${proposalLabel} — direction ${tail(proposal.direction_id || "unknown")}`,
            sub: parts.join(" — ") || "generation metadata not recorded",
          }),
        );
      });
      host.appendChild(proposalList);
    }
  }

  if (failureCards.length) {
    const failures = el("details", { class: "technical" }, [
      el("summary", { text: `D2 related failure records (${failureCards.length})` }),
    ]);
    failures.appendChild(
      el("div", {
        class: "prose mono",
        text: failureCards.map((item) => JSON.stringify(item)).join("\n"),
      }),
    );
    host.appendChild(failures);
  }
  const technicalD2 = el("details", { class: "technical" }, [
    el("summary", { text: "Complete D2 technical record" }),
  ]);
  technicalD2.appendChild(
    el("div", { class: "prose mono", text: JSON.stringify(d2, null, 2) }),
  );
  host.appendChild(technicalD2);

  host.appendChild(el("h3", { text: "D3 Choose an implementation direction" }));
  // D2 retains every generated candidate for audit.  D3 operates only on the
  // post-adherence/contraction support recorded by the selector trace.
  let directions = decision.direction_options.filter((option) => option.eligible);
  const usesIndex = directions.some((option) => option.index !== null);
  const recordedRanking = (decision.direction_ranking || {}).ranking;
  if (!usesIndex && Array.isArray(recordedRanking)) {
    const position = new Map(recordedRanking.map((candidateId, i) => [candidateId, i]));
    directions = [...directions].sort(
      (a, b) => (position.get(a.candidate_id) ?? Infinity) - (position.get(b.candidate_id) ?? Infinity),
    );
  }
  const dirScale = scaler(directions.map((option) => option.index));
  host.appendChild(
    hint(
      "D3 does not call the proposer again; it ranks and selects the eligible D2 candidates by ID. " +
        (!usesIndex
          ? "The first ranked candidate is selected deterministically."
          : dirScale.tied
            ? "The eligible candidates have the same UCB index, so the recorded tie-break selected one."
            : "Bar length is the optimistic UCB selection index, not a quality score."),
    ),
  );
  const dirList = el("div", { class: "ranks" });
  for (const row of trimmed(directions, (option, position) => {
    const estimate = option.estimate || {};
    const parts = [];
    if (!usesIndex) {
      parts.push(`ranked ${position + 1}/${directions.length}`);
    }
    if (estimate.n !== undefined) parts.push(`${estimate.successes ?? 0}/${estimate.n} succeeded`);
    if (option.match_distance !== null && option.match_distance !== undefined) {
      parts.push(`distance ${fmt(option.match_distance)}`);
    }
    if (option.adherence_status && option.adherence_status !== "adherent") {
      parts.push(humanize(option.adherence_reason || option.adherence_status));
    }
    if (option.slot_kind) parts.push(`${humanize(option.slot_kind)} slot`);
    if (option.match_kind) parts.push(`${humanize(option.match_kind)} semantic match`);
    const d2Label = d2DirectionLabels.get(option.direction.direction_id) || "D2 candidate";
    return rank({
      fraction: usesIndex ? dirScale.of(option.index) : 0,
      pick: option.selected,
      out: !option.eligible,
      disc: option.slot_kind === "discover",
      name: `${d2Label} → D3 realization: ${readable(option.direction.implementation_direction)}`,
      sub: parts.join(" — ") || "no prior observations",
    });
  })) {
    dirList.appendChild(row);
  }
  host.appendChild(dirList);
  const technicalD3 = el("details", { class: "technical" }, [
    el("summary", { text: "Complete D3 technical record" }),
  ]);
  technicalD3.appendChild(
    el("div", {
      class: "prose mono",
      text: JSON.stringify(action.decision_trace?.d3 || {}, null, 2),
    }),
  );
  host.appendChild(technicalD3);
}

function drawDirection(host, action) {
  const direction = action.direction;
  host.appendChild(el("h3", { text: "Failure mechanism" }));
  host.appendChild(el("div", { class: "prose", text: direction.failure_mechanism }));
  host.appendChild(el("h3", { text: "Implementation direction" }));
  host.appendChild(el("div", { class: "prose", text: direction.implementation_direction }));

  host.appendChild(el("h3", { text: "Edit boundary" }));
  const targets = el("div", { class: "kv" });
  for (const target of directionScope(direction)) {
    targets.appendChild(el("span", { class: "k", text: target.path }));
    targets.appendChild(
      el("span", {
        class: "v",
        text: [target.kind, (target.symbols || []).join(", ") || target.role].filter(Boolean).join(" — "),
      }),
    );
  }
  host.appendChild(targets);
  host.appendChild(el("h3", { text: "Resolved files" }));
  host.appendChild(el("div", {
    class: "prose mono",
    text: (direction.resolved_scope || []).join("\n") || "not recorded",
  }));

  host.appendChild(el("h3", { text: "Provenance" }));
  const provenance = el("div", { class: "kv" });
  const facts = [
    ["policy", policyLabel(state.data.meta.method, true)],
    ["slot", action.slot_kind || "—"],
    ["semantic match", action.match_kind || "—"],
    [
      "match distance",
      action.match_distance === null ? "no neighbour within threshold" : fmt(action.match_distance),
    ],
    ["arm", action.arm_id ? tail(action.arm_id) : "not applicable"],
    ["cited evidence", direction.evidence_refs.join(", ") || "—"],
    ["direction id", direction.direction_id],
  ];
  for (const [key, value] of facts) {
    provenance.appendChild(el("span", { class: "k", text: key }));
    provenance.appendChild(el("span", { class: "v", text: value }));
  }
  host.appendChild(provenance);

  const execution = action.execution;
  host.appendChild(el("h3", { text: "Execution" }));
  const exec = el("div", { class: "kv" });
  for (const [key, value] of [
    ["worker", execution.worker_status || "—"],
    ["adherence", execution.adherence || "—"],
    ["result commit", tail(execution.result_commit_sha)],
    ["touched files", execution.touched_files.join(", ") || "not recorded"],
  ]) {
    exec.appendChild(el("span", { class: "k", text: key }));
    exec.appendChild(el("span", { class: "v", text: value }));
  }
  host.appendChild(exec);
}

function drawEvaluation(host, action) {
  const evaluation = action.evaluation;
  const cellIds = Object.keys(evaluation.cell_results || {});

  host.appendChild(el("h3", { text: "Paired measurement" }));
  const measurement = el("div", { class: "kv" });
  for (const [key, value] of [
    ["challenger score", metricValue(evaluation.child_primary_value)],
    ["paired change", points(evaluation.primary_delta_mean, 2)],
    ["constraints", evaluation.constraints_pass === null ? "not recorded" : evaluation.constraints_pass ? "pass" : "fail"],
  ]) {
    measurement.appendChild(el("span", { class: "k", text: key }));
    measurement.appendChild(el("span", { class: "v", text: value }));
  }
  host.appendChild(measurement);

  host.appendChild(
    el("div", {
      class: "outcome-banner",
    }, [
      el("div", {}, [
        el("div", {
          class: "lead",
          text: action.prehistory?.warm_start_target
            ? `Warm-start accepted from ${action.prehistory.source_run_id}`
            : evaluation.admitted === null
            ? evaluation.admission_fate === "admitted"
              ? "Admission passed; archive update pending"
              : `${lifecycleLabel(action)}: archive decision pending`
            : evaluation.admitted
              ? "Added to the archive"
              : "Not added to the archive",
        }),
        el("div", {
          class: "detail",
          text: action.prehistory?.warm_start_target
            ? `Original source decision: ${humanize(evaluation.admission_fate || "not recorded")}`
            : humanize(evaluation.admission_fate || "decision not recorded"),
        }),
      ]),
      el("span", {
        class: `badge ${archiveVerdict(action).className}`,
        text: archiveVerdict(action).label,
      }),
    ]),
  );

  host.appendChild(el("h3", { text: "Evaluation cases" }));
  if (!cellIds.length) {
    host.appendChild(
      el("div", {
        class: "empty",
          text: action.lifecycle_stage === "blocked"
          ? "Implementation stopped before B-tier measurement, so there are no evaluation cases."
          : action.lifecycle_stage === "aborted"
            ? "The experiment stopped before this attempt produced B-tier case results."
          : "B-tier measurement has not produced any case results for this attempt yet.",
      }),
    );
    return;
  }
  host.appendChild(
    hint(
      `These ${cellIds.length} B-tier cases calculate the challenger score. Green means solved correctly and red means answered incorrectly. Full B questions are intentionally not copied into the run inspector.`,
    ),
  );
  const chipFor = (value) =>
    el("span", {
      class: `chip ${value === true ? "pass" : value === false ? "fail" : "pending"}`,
      text: value === true ? "Correct" : value === false ? "Incorrect" : "Not measured",
    });
  const grid = el("div", { class: "cellgrid" });
  for (const cellId of cellIds) {
    grid.appendChild(
      el("div", { class: "cellrow single" }, [
        el("span", { class: "task", text: cellLabel(cellId), title: cellId }),
        chipFor(evaluation.cell_results[cellId]),
      ]),
    );
  }
  host.appendChild(grid);

  const notes = Object.entries(evaluation.cell_notes || {});
  if (notes.length) {
    host.appendChild(el("h3", { text: "Cell notes" }));
    const list = el("div", { class: "kv" });
    for (const [cellId, note] of notes) {
      list.appendChild(el("span", { class: "k", text: cellId }));
      list.appendChild(el("span", { class: "v", text: note }));
    }
    host.appendChild(list);
  }

  const timeoutDiagnostics = Object.entries(evaluation.cell_diagnostics || {});
  if (timeoutDiagnostics.length) {
    host.appendChild(el("h3", { text: "Timeout diagnostics (safe aggregate)" }));
    const list = el("div", { class: "kv" });
    for (const [cellId, diagnostic] of timeoutDiagnostics) {
      const first = diagnostic.first_response_latency_seconds;
      const last = diagnostic.last_terminal_activity_seconds;
      const providerEnvelope = diagnostic.provider_diagnostics;
      const provider = providerEnvelope?.provider;
      const summary = [
        diagnostic.side,
        `${diagnostic.response_count} model responses`,
        `first ${first === null || first === undefined ? "not recorded" : `${fmt(first, 1)}s`}`,
        `last terminal ${last === null || last === undefined ? "not recorded" : `${fmt(last, 1)}s`}`,
        `answer before timeout ${diagnostic.answer_written_before_timeout ? "yes" : "no"}`,
        `provider waiting ${diagnostic.provider_waiting_at_deadline ? "yes" : "no"}`,
        ...(provider
          ? [
              `worker deadline ${providerEnvelope.worker_deadline_reached ? "yes" : "no"}`,
              `broker ${provider.responses_finished}/${provider.logical_requests_started} responses`,
              `${fmt(provider.total_upstream_wait_seconds, 1)}s upstream wait`,
              `${fmt(provider.terminal_transport_wait_seconds, 1)}s terminal transport union`,
              `${fmt(provider.nonproductive_wait_seconds, 1)}s nonproductive wall union`,
              `${provider.safe_retries} safe retries`,
            ]
          : []),
      ].join(" | ");
      list.appendChild(el("span", { class: "k", text: cellId }));
      list.appendChild(el("span", { class: "v", text: summary }));
    }
    host.appendChild(list);
  }

  const technical = el("details", { class: "technical" });
  technical.appendChild(el("summary", { text: "Policy feedback and measurement metadata" }));
  const facts = el("div", { class: "kv", style: { marginTop: "12px" } });
  for (const [key, value] of [
    ["policy feedback", action.effectiveness_label || "not recorded"],
    ["policy statistic", fmt(evaluation.signed_statistic, 4)],
    ["policy margin", fmt(action.effectiveness_margin, 4)],
    ["feedback reason", humanize(action.effectiveness_reason || "not recorded")],
    ["conservative lower", points(evaluation.primary_delta_lower, 2)],
    ["evidence tier", humanize(evaluation.evidence_tier || "not recorded")],
    ["validity", humanize(evaluation.validity || "not recorded")],
    ["attribution", humanize(evaluation.attribution || "not recorded")],
    ["child commit", evaluation.child_commit_sha || "not recorded"],
  ]) {
    facts.appendChild(el("span", { class: "k", text: key }));
    facts.appendChild(el("span", { class: "v", text: value }));
  }
  technical.appendChild(facts);
  host.appendChild(technical);
}

/* ---------- wiring ---------- */

document.getElementById("run-select").addEventListener("change", (event) => {
  loadRun(event.target.value);
});
document.getElementById("drawer-close").addEventListener("click", closeDrawer);
overlay.addEventListener("click", closeDrawer);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeDrawer();
});
window.addEventListener("resize", () => {
  if (state.data) render();
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshRun();
});
window.setInterval(refreshRun, 15000);

loadRuns();
