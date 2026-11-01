const runSelect = document.querySelector('#run');
const statusBox = document.querySelector('#status');
const content = document.querySelector('#content');
const diffDialog = document.querySelector('#diff');

const text = (value, fallback = '—') => value === null || value === undefined ? fallback : String(value);
const scoreVector = vector => (vector || []).map(item => `${item.benchmark_id}: ${text(item.score)}`).join(' · ') || '—';
const node = (tag, className, value) => { const element = document.createElement(tag); if (className) element.className = className; if (value !== undefined) element.textContent = value; return element; };

async function json(url) {
  const response = await fetch(url, {cache: 'no-store'});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function renderSummary(meta) {
  const root = document.querySelector('#summary'); root.replaceChildren();
  const values = [['Method', meta.method], ['Benchmarks', meta.benchmarks.join(', ')], ['Attempts', meta.attempts], ['Candidates', meta.candidates], ['Delivered', meta.terminal_candidates], ['Attempt cap', meta.attempt_cap]];
  for (const [label, value] of values) { const item = node('div', 'metric'); item.append(node('small', '', label), node('strong', '', text(value))); root.append(item); }
}

function renderAttempts(attempts) {
  const root = document.querySelector('#attempts'); root.replaceChildren();
  for (const attempt of attempts) { const item = node('article', 'card'); item.append(node('strong', '', `#${attempt.attempt_index} ${attempt.kind}`), node('p', 'muted', `${attempt.state} · gate ${text(attempt.gate_decision)}`), node('small', '', attempt.attempt_id)); root.append(item); }
  if (!attempts.length) root.append(node('p', 'muted', 'No attempts have been started.'));
}

function renderCandidates(runId, candidates) {
  const root = document.querySelector('#candidates'); root.replaceChildren();
  for (const candidate of candidates) {
    const row = document.createElement('tr');
    const usage = candidate.usage ? `${text(candidate.usage.input_tokens)} in / ${text(candidate.usage.output_tokens)} out · $${text(candidate.usage.cost_usd)}` : '—';
    const values = [`#${candidate.candidate_ordinal}\n${candidate.candidate_id}`, candidate.role, `#${candidate.attempt_index}`, text(candidate.outcome), scoreVector(candidate.score_vector), text(candidate.coverage), usage];
    for (const value of values) { const cell = document.createElement('td'); cell.textContent = value; row.append(cell); }
    const action = document.createElement('td'); const button = node('button', '', 'View diff');
    button.addEventListener('click', async () => {
      try { const base = candidate.diff.default_base; const query = new URLSearchParams({run_id: runId, candidate_id: candidate.candidate_id, base}); const payload = await json(`/api/diff?${query}`); document.querySelector('#diff-title').textContent = `${candidate.candidate_id} · ${payload.base_label}${payload.truncated ? ' · truncated' : ''}`; document.querySelector('#diff-text').textContent = payload.text || 'No changes.'; diffDialog.showModal(); } catch (error) { showError(error); }
    });
    action.append(button); row.append(action); root.append(row);
  }
}

function renderLineage(graph) {
  const root = document.querySelector('#lineage'); root.replaceChildren();
  for (const entry of graph.nodes) { const parent = graph.edges.find(edge => edge.target === entry.candidate_id); const item = node('div', 'item'); item.append(node('p', '', `#${entry.ordinal} · ${entry.role}`), node('small', '', parent ? `parent ${parent.source}` : 'root candidate')); root.append(item); }
  if (!graph.nodes.length) root.append(node('p', 'muted', 'No candidate lineage yet.'));
}

function renderDevelopment(rows) {
  const root = document.querySelector('#development'); root.replaceChildren();
  for (const entry of rows) { const item = node('div', 'item'); item.append(node('p', '', `${entry.benchmark_id} · ${entry.outcome}`), node('small', '', scoreVector(entry.score_vector))); root.append(item); }
  if (!rows.length) root.append(node('p', 'muted', 'No delivered development result.'));
}

function renderHeldOut(value) {
  const root = document.querySelector('#held-out'); root.replaceChildren();
  if (!value) { root.append(node('p', 'muted', 'Held-out evaluation has not been selected.')); return; }
  root.append(node('p', '', `${value.state} · ${text(value.outcome)}`), node('p', 'muted', scoreVector(value.score_vector)));
}

async function loadDetail() {
  if (!runSelect.value) return;
  statusBox.className = 'status'; statusBox.textContent = 'Validating canonical ledgers…'; statusBox.hidden = false; content.hidden = true;
  try { const detail = await json(`/api/runs/${encodeURIComponent(runSelect.value)}`); renderSummary(detail.meta); renderAttempts(detail.attempts); renderCandidates(detail.meta.run_id, detail.candidate_evaluations); renderLineage(detail.lineage_graph); renderDevelopment(detail.development); renderHeldOut(detail.held_out); statusBox.hidden = true; content.hidden = false; } catch (error) { showError(error); }
}

function showError(error) { statusBox.hidden = false; statusBox.className = 'status error'; statusBox.textContent = error instanceof Error ? error.message : String(error); }

async function initialize() {
  try { const catalogue = await json('/api/runs'); runSelect.replaceChildren(); for (const run of catalogue.runs) { const option = document.createElement('option'); option.value = run.run_id; option.textContent = `${run.method} · ${run.benchmarks.join(' + ')} · ${run.run_id}`; runSelect.append(option); } await loadDetail(); } catch (error) { showError(error); }
}

document.querySelector('#refresh').addEventListener('click', initialize);
runSelect.addEventListener('change', loadDetail);
initialize();
