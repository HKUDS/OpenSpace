/* Research projections share the inspector's charts, drawer, helpers and styles. */
function researchFacts(host, entries) {
  const facts = el('div', {class: 'overview-grid'});
  for (const [key, value] of entries) {
    facts.appendChild(el('div', {class: 'overview-fact'}, [
      el('span', {class: 'k', text: key}),
      el('span', {class: 'v', text: value ?? 'Not recorded'}),
    ]));
  }
  host.appendChild(facts);
}

function researchJSON(host, title, value) {
  host.appendChild(el('details', {class: 'technical'}, [
    el('summary', {text: title}),
    el('pre', {class: 'prose mono', text: JSON.stringify(value, null, 2)}),
  ]));
}

function researchAge(timestamp) {
  if (typeof timestamp !== 'number') return 'No result yet';
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - timestamp));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m ago`;
}

function researchProgressText(p) {
  if (!p?.available) return 'Waiting for measurement plan';
  return `${p.completed}/${p.total} complete · ${p.running ?? '—'} executing · ${p.queued ?? '—'} queued`;
}

function researchProgress(host, p) {
  if (!p) return;
  if (!p.available) { host.appendChild(hint(p.reason)); return; }
  researchFacts(host, [
    ['Task executions', `${p.completed} / ${p.total}`],
    ['Executing now', p.running ?? 'Not recorded per comparison'],
    ['Queued', p.queued ?? 'Not recorded per comparison'],
    ['Waiting for a shared slot', p.waiting_for_slot ?? 'Not recorded'],
    ['Latest task result', researchAge(p.last_result_at)],
    ['Original program', `${p.versions.control.completed} / ${p.versions.control.total}`],
    ['Modified program', `${p.versions.candidate.completed} / ${p.versions.candidate.total}`],
    ['Shared task capacity', p.shared_capacity ?? 'Not recorded'],
  ]);
  if (p.needs_recovery) host.appendChild(hint(`${p.needs_recovery} dispatched task(s) have no result and need inspection.`));
  if (p.resuming) host.appendChild(hint('Continuing the interrupted comparison using saved task results.'));
  if (p.pending) host.appendChild(hint(`${p.pending} tasks remain undispatched while this comparison is stopped.`));
  if (p.error_type) host.appendChild(hint(`Execution stopped: ${p.error_type}. Detailed diagnostics are retained in the private journal.`));
  if (p.invalid_originals || p.recovery) {
    const retry = p.recovery || {};
    researchFacts(host, [['Invalid original measurements', p.invalid_originals || 0],
      ['Replacement attempts', `${retry.completed ?? 0} / ${retry.planned ?? 'not scheduled'}`],
      ['Valid replacements', retry.recovered ?? 0],
      ['Replacement activity', `${retry.running ?? 0} executing · ${retry.queued ?? 0} queued`]]);
    if (retry.exhausted) host.appendChild(hint(`${retry.exhausted} replacement(s) remained invalid; the retry limit was reached.`));
    if (retry.needs_inspection) host.appendChild(hint('A replacement has no durable receipt; automatic resubmission is blocked.'));
  }
  if (p.telemetry === 'legacy-slots') host.appendChild(hint('Execution count is read from live shared-slot ownership. Older records do not identify each task’s execution start.'));
  if (p.telemetry === 'owner-only') host.appendChild(hint(`This process holds ${p.owner_slots ?? 'an unknown number of'} shared slots across several operations; per-comparison execution counts are unavailable.`));
}

function renderResearch() {
  const {research: r, actions, meta} = state.data;
  document.getElementById('research-panel').hidden = false;
  document.getElementById('attempts-title').textContent = 'Implementations';
  document.getElementById('exp-sub').textContent =
    `${humanize(meta.environment)} · ${r.sessions.length} research sessions · ${r.proposer_turn} proposer turns`;
  const status = document.getElementById('run-status');
  status.textContent = r.baseline.status === 'running' ? 'Collecting baseline' :
    r.baseline.status === 'draining' ? 'Finishing active baseline tasks' :
    r.baseline.status === 'paused' ? 'Baseline paused' :
    r.status === 'provider_blocked' ? 'Blocked: provider credits' : humanize(r.status);
  status.className = `pill${['failed','blocked','unresolved','provider_blocked'].includes(r.status) ? ' problem' : ''}`;
  document.getElementById('refresh-time').textContent = `Refreshed ${new Date().toLocaleTimeString()}`;
  document.getElementById('progress-intro').textContent =
    'Each implementation shows its latest complete, bound B comparison. Samples can differ; the blue envelope is descriptive. Local experiments do not supply these scores.';
  document.getElementById('protocol-note').textContent = 'A supports research; B supplies restricted comparisons; C stays sealed.';
  document.getElementById('curve-note').textContent = `${actions.filter(a => a.evaluation.completed_at).length} measured implementations · bands group research sessions`;
  for (const marker of document.querySelectorAll('.legend .swatch.generation')) marker.parentElement.hidden = true;
  const stats = document.getElementById('stats');
  clear(stats);
  const active = r.jobs.filter(j => ['queued','running'].includes(j.status));
  stats.appendChild(statCard('Active experiments', active.length,
    `${active.filter(j => j.status === 'running').length} running · ${active.filter(j => j.status === 'queued').length} queued`));
  stats.appendChild(statCard('Implementations', actions.length,
    `${actions.filter(a => a.evaluation.completed_at).length} measured on B`));
  stats.appendChild(statCard('Accepted programs', state.data.summary.total_admitted,
    `${r.feedback_count} settled strategy feedback records`));
  stats.appendChild(statCard(state.data.summary.externally_billed_workers ? 'Known research cost' : 'Research cost', `$${fmt(state.data.summary.total_cost_usd, 3)}`,
    r.budget_mode === 'actual' ? `Reservations disabled${r.unknown ? ` · ${r.unknown} receipts pending (nonblocking)` : ''}` :
    `$${fmt(r.reserved, 3)} reserved${r.unknown ? ` · ${r.unknown} need reconciliation` : ''}`));
  const comparisons = r.jobs.filter(j => j.kind === 'admit' && (['queued','running'].includes(j.status) || j.progress?.resuming) && j.progress?.available);
  if (comparisons.length) {
    const complete = comparisons.reduce((n,j) => n+j.progress.completed, 0);
    const total = comparisons.reduce((n,j) => n+j.progress.total, 0);
    const running = comparisons.every(j => j.progress.running !== null) ? comparisons.reduce((n,j) => n+j.progress.running, 0) : null;
    stats.appendChild(statCard('Current B task executions', `${complete} / ${total}`,
      `${running ?? '—'} executing · latest result ${researchAge(Math.max(...comparisons.map(j => j.progress.last_result_at || 0)) || null)}`));
  }
  const baseline = document.getElementById('research-baseline');
  clear(baseline);
  if (r.status === 'provider_blocked' && r.provider_blocker?.message) {
    baseline.appendChild(el('div', {class:'run-alert', text:r.provider_blocker.message}));
  }
  researchFacts(baseline, [
    ['Shared A baseline', humanize(r.baseline.status)],
    ['Completed runs', `${r.baseline.completed ?? 0} / ${r.baseline.total ?? 'not started'}`],
    ['Passed', r.baseline.passed ?? 'Not measured'],
    ['Shared baseline cost', typeof r.baseline.spent === 'number' ? `$${fmt(r.baseline.spent, 3)}` : 'Not recorded'],
    ['Pending accounting', r.baseline.pending_cost_calls ? `${r.baseline.pending_cost_calls} calls · $${fmt(r.baseline.reserved,3)} held (estimate)` : 'No pending invoices recorded'],
  ]);
  if (r.baseline.error) baseline.appendChild(el('div', {class:'run-alert', text:r.baseline.error}));
  if (r.baseline.recovery) {
    const b = r.baseline.recovery;
    baseline.appendChild(hint(`${b.original_invalid} invalid original run(s) retained · ${b.completed}/${b.planned} replacements complete, ${b.valid} valid. Completed runs counts the selected attempts; costs include all attempts.`));
  }
  document.getElementById('research-note').textContent =
    'Builds, local experiments and B comparisons can run independently. Baseline cost is shared across policies.';
  const jobs = document.getElementById('research-jobs');
  clear(jobs);
  if (!r.jobs.length) jobs.appendChild(el('div', {class: 'empty', text: 'No experiments dispatched yet.'}));
  const tracks = new Map(r.tracks.map(t => [t.id, t]));
  for (const line of r.lines || []) {
    const section = el('details', {class: 'technical'});
    section.appendChild(el('summary', {text: `${line.track} · ${line.label} · ${humanize(line.state)}`}));
    if (line.latest_judgment) {
      section.appendChild(el('div', {class: 'prose', text: line.latest_judgment.text}));
      researchFacts(section, [['Judgment source version', line.latest_judgment.source_commit || 'Not recorded'],
        ['Supporting tickets', (line.latest_judgment.evidence_tickets || []).join(', ') || 'Not cited']] );
    } else {
      section.appendChild(hint('No proposer judgment recorded yet. Experiment completion alone does not establish a finding.'));
    }
    for (const experiment of line.experiments) {
      section.appendChild(el('a', {href: `#research-job-${experiment.ticket}`, text: `${experiment.ticket} · ${humanize(experiment.status)} · ${experiment.question}`}));
      if (experiment.reported_result) section.appendChild(el('div', {class:'prose', text: `Worker report: ${experiment.reported_result}`}));
    }
    if (line.omitted_experiments) section.appendChild(hint(`${line.omitted_experiments} earlier experiments remain in the job history below.`));
    jobs.appendChild(section);
  }
  for (const job of [...r.jobs].reverse()) {
    const track = tracks.get(job.track_id);
    const row = el('details', {class: 'technical', id: `research-job-${job.id}`});
    row.open = state.researchOpen.has(job.id);
    row.addEventListener('toggle', () => {
      if (!row.isConnected) return;
      if (row.open) state.researchOpen.add(job.id); else state.researchOpen.delete(job.id);
    });
    row.appendChild(el('summary', {text:
      `${job.kind === 'admit' ? 'B comparison' : humanize(job.kind)} · ${humanize(job.status)} · ${job.label}${job.progress ? ' · ' + researchProgressText(job.progress) : ''}`}));
    researchProgress(row, job.progress);
    researchFacts(row, [['Ticket', job.id], ['Direction', track?.label || job.track_id],
      ['Direction state', track?.state || 'Not recorded'], ['Cost', typeof job.cost_usd === 'number' ? `$${fmt(job.cost_usd, 3)}` :
        job.worker_accounting?.mode === 'external' ? 'Externally billed · cost not reported' : 'Pending settlement']]);
    if (job.instruction) row.appendChild(el('div', {class: 'prose', text: job.instruction}));
    if (job.summary) row.appendChild(el('div', {class: 'prose', text: job.summary}));
    if (job.error) row.appendChild(el('div', {class: 'run-alert', text: job.error}));
    if (track?.state === 'dropped') row.appendChild(hint('Further investment stopped; dispatched jobs still settle.'));
    if (state.index.actionById.has(job.id)) row.appendChild(el('button', {
      class: 'filter', text: 'Open implementation', onclick: () => openDrawer(job.id),
    }));
    jobs.appendChild(row);
  }
  // Research updates happen as results settle, not at legacy batch barriers.
  document.getElementById('strategy-overview').style.display = 'none';
  document.getElementById('policy-panel').style.display = '';
  document.getElementById('policy-title').textContent = policyLabel(meta.method, true);
  document.getElementById('policy-note').textContent = `${r.feedback_count} feedback records`;
  document.getElementById('policy-intro').textContent =
    'Strategy feedback follows settled build and B comparison results. Local test success is not a UCB reward.';
  const policy = document.getElementById('chart-policy');
  clear(policy);
  if (meta.method === 'memory') {
    policy.appendChild(el('div', {class: 'prose', text: r.memory || 'No Policy Memory update recorded yet.'}));
  }
  for (const action of actions.filter(a => a.research.feedback.reason)) {
    policy.appendChild(el('div', {class: 'policy-row'}, [
      el('button', {class: 'filter', text: `Implementation ${attemptNumber(action)}`, onclick: () => openDrawer(action.action_id)}),
      el('span', {text: `${humanize(action.effectiveness_label || 'unscored')} · ${action.research.feedback.reason}`}),
    ]));
  }
  if (!policy.childElementCount) policy.appendChild(hint('No settled strategy feedback yet.'));
  const selection = document.getElementById('chart-selection');
  clear(selection);
  document.getElementById('sel-note').textContent = 'Recorded selection at dispatch';
  clear(document.getElementById('sel-legend'));
  for (const action of actions) {
    selection.appendChild(el('div', {class: 'policy-row'}, [
      el('button', {class: 'filter', text: `Implementation ${attemptNumber(action)}`, onclick: () => {
        openDrawer(action.action_id); state.drawerTab = 'decision'; renderDrawer();
      }}),
      el('span', {text: `${tail(action.parent_commit_sha || '')} · ${action.slot_kind || policyLabel(meta.method)} · ${directionHeadline(action.direction.implementation_direction)}`}),
    ]));
  }
  if (!actions.length) selection.appendChild(hint('No implementation selected yet.'));
}

function drawResearchDetail(host, action) {
  const r = action.research;
  if (state.drawerTab === 'decision') {
    researchFacts(host, [['Mechanism family', action.arm_id], ['Selection kind', action.slot_kind],
      ['Repeated implementation', r.semantic_repeat_of || 'No repeat recorded'],
      ['Strategy result', action.effectiveness_label || 'Pending'], ['Reason', r.feedback.reason]]);
    researchJSON(host, 'Parent selection', r.parent_selection);
    researchJSON(host, 'Implementation selection', r.selection);
    return;
  }
  if (state.drawerTab === 'summary') {
    host.appendChild(el('div', {class: 'prose', text: action.direction.implementation_direction}));
    researchFacts(host, [['Status', lifecycleLabel(action)], ['Parent', action.parent_commit_sha],
      ['Commit', action.execution.result_commit_sha], ['Files', action.execution.touched_files.join(', ')],
      ['Worker cost', typeof action.execution.cost_usd === 'number' ? `$${fmt(action.execution.cost_usd, 3)}` :
        action.research?.job?.worker_accounting?.mode === 'external' ? 'Externally billed · cost not reported' : 'Pending'],
      ['Direction', r.job.track_id]]);
    host.appendChild(el('div', {class: 'prose', text: r.job.summary}));
    if (r.job.error) host.appendChild(hint(r.job.error));
  }
  host.appendChild(el('h3', {text: 'B comparison history'}));
  if (!r.comparisons.length) host.appendChild(hint('No B comparison recorded. A completed build or local test does not establish task improvement.'));
  for (const c of r.comparisons) {
    const block = el('div', {class: 'technical'});
    block.appendChild(el('strong', {text: `${c.ticket} · ${humanize(c.status)} · ${c.valid ? c.admission : 'not a complete bound comparison'}`}));
    researchProgress(block, c.progress);
    researchFacts(block, [['Control commit', c.control_commit], ['Candidate commit', c.candidate_commit],
      ['B success rate', pct(c.value)], ['Paired change', points(c.delta)],
      ['Conservative bound', points(c.lower)],
      ['Coverage', `${c.coverage.measured_blocks ?? '—'} / ${c.coverage.expected_blocks ?? '—'}`]]);
    if (c.reason) block.appendChild(el('div', {class: 'prose', text: c.reason}));
    host.appendChild(block);
  }
}
