import {state} from '../state.js';
import {el,button,heading,empty} from '../lib/html.js';
import {icon,registerIcons} from '../lib/icons.js';
import {probeSummary,reachabilityBadge,checkedLine,loadingState,loadFailure,cachedProbe,rememberProbe,checkConnection,instanceStats,fleetMetrics} from '../lib/payload.js';

registerIcons({
  server:'M4 6h16M4 10h16M4 14h16M4 18h16 M2 4a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V4z M6 6h.01 M6 10h.01 M6 14h.01 M6 18h.01',
  shield:'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z',
  alert:'M12 9v4 M12 17h.01 M10.3 2.8 1.8 17.5A2 2 0 0 0 3.5 20.5h17a2 2 0 0 0 1.7-3L13.7 2.8a2 2 0 0 0-3.4 0z'
});

function latencyTier(ms) {
  if (!Number.isFinite(ms)) return '';
  if (ms < 150) return 'latency-fast';
  if (ms < 500) return 'latency-moderate';
  return 'latency-slow';
}

function classifyProbe(result) {
  if (!result || typeof result.reachable !== 'boolean') return 'untested';
  if (!result.reachable) return 'unreachable';
  const caps = result.capabilities || {};
  const isOperational = caps.workflow_read === 'supported' && caps.execution_read === 'supported' && (!result.diagnostics || !result.diagnostics.length);
  return isOperational ? 'operational' : 'degraded';
}

export async function render(container) {
  const controller = new AbortController();
  let round = null;
  let currentFilter = 'all'; // 'all' | 'issues'

  // Metric strip elements
  const totalNum = el('p', {class: 'metric-number'}, String(state.instances.length));
  const operationalNum = el('p', {class: 'metric-number'}, '0');
  const degradedNum = el('p', {class: 'metric-number'}, '0');
  const unreachableNum = el('p', {class: 'metric-number'}, '0');
  const latencyNum = el('p', {class: 'metric-number'}, '—');

  const rollupStrip = el('div', {class: 'fleet-summary-strip', role: 'region', 'aria-label': 'Fleet health metrics'},
    el('div', {class: 'fleet-metric-cell'}, el('h3', {}, 'Total instances'), totalNum),
    el('div', {class: 'fleet-metric-cell metric-operational'}, el('h3', {}, 'Operational'), operationalNum),
    el('div', {class: 'fleet-metric-cell metric-degraded'}, el('h3', {}, 'Degraded'), degradedNum),
    el('div', {class: 'fleet-metric-cell metric-unreachable'}, el('h3', {}, 'Unreachable'), unreachableNum),
    el('div', {class: 'fleet-metric-cell'}, el('h3', {}, 'Fleet avg latency'), latencyNum)
  );

  const statusAnnouncer = el('div', {class: 'sr-only', role: 'status', 'aria-live': 'polite'});
  const content = el('div', {class: 'grid', 'aria-label': 'Fleet instances'});

  // Refresh & filter controls
  const refresh = button('Check fleet', () => {
    if (round) round.abort();
    else start();
  }, 'ghost');

  const filterAllBtn = button(`All instances (${state.instances.length})`, () => setFilter('all'), 'ghost fleet-filter-btn', {'aria-pressed': 'true'});
  const filterIssuesBtn = button('Issues only (0)', () => setFilter('issues'), 'ghost fleet-filter-btn', {'aria-pressed': 'false'});

  const filterGroup = el('div', {class: 'fleet-filter-group'}, filterAllBtn, filterIssuesBtn);
  const toolbar = el('div', {class: 'fleet-toolbar'}, filterGroup, refresh);

  container.append(
    heading('Fleet health', 'Reachability, latency and throughput for every instance.'),
    statusAnnouncer,
    rollupStrip,
    toolbar,
    content
  );

  if (!state.instances.length) {
    content.append(empty('Connect your first instance', 'Add an n8n instance to check fleet health.', [el('a', {class: 'button ghost', href: '#instances'}, 'Open instances')]));
    refresh.disabled = true;
    filterAllBtn.disabled = true;
    filterIssuesBtn.disabled = true;
    return () => controller.abort();
  }

  function renderDiagnosticGuidance(result) {
    if (!result || result.reachable && (!result.diagnostics || !result.diagnostics.length)) return null;

    let guidance = null;
    const hasDiag = code => result.diagnostics?.some(d => d.code === code);

    if (!result.reachable) {
      if (hasDiag('probe_timeout') || hasDiag('upstream_timeout')) {
        guidance = el('div', {class: 'instance-diagnostic-box is-unreachable'},
          icon('alert'),
          el('div', {},
            el('strong', {}, 'Connection timed out (10s limit). '),
            'n8n did not respond. Check if the container is running in ',
            el('a', {href: '#containers'}, 'Containers'),
            ' or verify your network ingress and DNS resolution.'
          )
        );
      } else {
        guidance = el('div', {class: 'instance-diagnostic-box is-unreachable'},
          icon('alert'),
          el('div', {},
            el('strong', {}, 'Instance is unreachable. '),
            'Inference cannot establish a network connection. Verify the endpoint URL in ',
            el('a', {href: '#instances'}, 'Instances'),
            '.'
          )
        );
      }
    } else if (hasDiag('upstream_auth_failed')) {
      guidance = el('div', {class: 'instance-diagnostic-box'},
        icon('shield'),
        el('div', {},
          el('strong', {}, 'Authentication failed. '),
          'n8n rejected the configured API key. Update or re-enter the API key in ',
          el('a', {href: '#instances'}, 'Instances'),
          '.'
        )
      );
    } else if (hasDiag('upstream_forbidden')) {
      guidance = el('div', {class: 'instance-diagnostic-box'},
        icon('shield'),
        el('div', {},
          el('strong', {}, 'Access forbidden. '),
          'The API key lacks read permissions for workflows or executions. Ensure the key has admin/read scope in n8n.'
        )
      );
    }
    return guidance;
  }

  function resultView(result, entry) {
    const {list, notes, diagnostics} = probeSummary(result);
    const guidance = renderDiagnosticGuidance(result);

    const actionsRow = el('div', {class: 'instance-actions-row'},
      button('Re-check', () => start([entry]), 'ghost', {'aria-label': `Re-check connection for ${entry.instance.name}`}),
      el('a', {class: 'button ghost', href: '#instances'}, 'Manage connection'),
      el('a', {class: 'button ghost', href: '#workflows'}, 'Workflows'),
      el('a', {class: 'button ghost', href: '#errors'}, 'Errors')
    );

    // Throughput and error rate first, then API access as chips; warnings appear only when something is wrong.
    return el('div', {class: 'stack instance-body'},
      entry.stats,
      el('div', {class: 'capability-row'}, el('span', {class: 'capability-row-label'}, 'API access'), list),
      guidance,
      diagnostics.length ? notes : null,
      el('p', {class: 'instance-checked', title: result.checked_at}, checkedLine(result)),
      actionsRow
    );
  }

  function updateEntryUI(entry, result) {
    entry.currentResult = result;
    const classification = classifyProbe(result);

    entry.card.classList.remove('is-unreachable', 'is-degraded');
    if (classification === 'unreachable') entry.card.classList.add('is-unreachable');
    else if (classification === 'degraded') entry.card.classList.add('is-degraded');

    entry.statusBadge.replaceChildren(reachabilityBadge(result));

    if (result && Number.isFinite(result.latency_ms)) {
      const tier = latencyTier(result.latency_ms);
      entry.latencyBadge.className = `latency-pill ${tier}`.trim();
      entry.latencyBadge.textContent = `${result.latency_ms} ms`;
      entry.latencyBadge.hidden = false;
    } else {
      entry.latencyBadge.hidden = true;
    }

    entry.body.replaceChildren(resultView(result, entry));
    entry.feedback.replaceChildren();

    updateRollup();
    applyFilter();
  }

  function updateRollup() {
    let opCount = 0, degCount = 0, unreachCount = 0;
    let latencySum = 0, latencyCount = 0;

    for (const entry of entries) {
      const classification = classifyProbe(entry.currentResult);
      if (classification === 'operational') opCount++;
      else if (classification === 'degraded') degCount++;
      else if (classification === 'unreachable') unreachCount++;

      if (entry.currentResult && Number.isFinite(entry.currentResult.latency_ms)) {
        latencySum += entry.currentResult.latency_ms;
        latencyCount++;
      }
    }

    operationalNum.textContent = String(opCount);
    degradedNum.textContent = String(degCount);
    unreachableNum.textContent = String(unreachCount);

    const issuesTotal = degCount + unreachCount;
    filterIssuesBtn.textContent = `Issues only (${issuesTotal})`;

    if (latencyCount > 0) {
      latencyNum.textContent = `${Math.round(latencySum / latencyCount)} ms`;
    } else {
      latencyNum.textContent = '—';
    }
  }

  function setFilter(filter) {
    currentFilter = filter;
    filterAllBtn.setAttribute('aria-pressed', String(filter === 'all'));
    filterIssuesBtn.setAttribute('aria-pressed', String(filter === 'issues'));
    applyFilter();
  }

  function applyFilter() {
    for (const entry of entries) {
      if (currentFilter === 'all') {
        entry.card.hidden = false;
      } else {
        const classification = classifyProbe(entry.currentResult);
        entry.card.hidden = !(classification === 'degraded' || classification === 'unreachable');
      }
    }
  }

  const entries = state.instances.map(instance => {
    const cached = cachedProbe(instance);
    const body = el('div');
    const feedback = el('div');
    const statusBadge = el('span', {class: 'instance-status-wrap'});
    const latencyBadge = el('span', {class: 'latency-pill', hidden: true});

    const headRow = el('div', {class: 'instance-head-row'},
      el('div', {class: 'instance-title-area'},
        el('h2', {}, instance.name),
        el('span', {class: 'instance-url'}, instance.url)
      ),
      el('div', {class: 'instance-status-badges'}, statusBadge, latencyBadge)
    );

    const card = el('article', {
      class: 'card stack decision-card instance-card',
      'aria-label': `Health of ${instance.name}`
    }, headRow, feedback, body);

    content.append(card);

    const entry = {
      instance,
      card,
      body,
      feedback,
      statusBadge,
      latencyBadge,
      currentResult: cached,
      // Filled when the fleet scan returns; dashes until then, never zeros.
      stats: el('div', {class: 'instance-stats-slot'}, instanceStats(null))
    };

    if (!cached) {
      feedback.append(loadingState(`Checking connections on ${instance.name}…`));
    }

    return entry;
  });

  // Cached cards update fleet totals and filters, which need the complete list.
  for (const entry of entries) {
    if (entry.currentResult) updateEntryUI(entry, entry.currentResult);
  }
  updateRollup();

  function start(selected = entries) {
    if (controller.signal.aborted || !selected.length) return;

    const isFleetRound = selected.length === entries.length;
    if (isFleetRound && round) return;

    const current = new AbortController();
    if (isFleetRound) {
      round = current;
      refresh.textContent = 'Cancel';
      statusAnnouncer.textContent = 'Checking fleet connection health across all instances…';
    }

    const cancel = () => current.abort();
    controller.signal.addEventListener('abort', cancel, {once: true});

    selected.forEach(entry => {
      entry.feedback.replaceChildren(loadingState(`Checking connections on ${entry.instance.name}…`));
    });

    let index = 0;
    async function worker() {
      while (index < selected.length && !current.signal.aborted && !controller.signal.aborted) {
        const entry = selected[index++];
        try {
          const result = await checkConnection(entry.instance, {signal: current.signal});
          if (current.signal.aborted || controller.signal.aborted) return;
          rememberProbe(entry.instance, result);
          updateEntryUI(entry, result);
        } catch (error) {
          if (current.signal.aborted || controller.signal.aborted) return;
          const failure = loadFailure(error, () => start([entry]), `Could not check ${entry.instance.name}`);
          const retryBtn = failure.querySelector('button');
          if (retryBtn) retryBtn.disabled = isFleetRound && Boolean(round);
          entry.feedback.replaceChildren(failure);
        }
      }
    }

    const concurrency = Math.min(3, selected.length);
    Promise.all(Array.from({length: concurrency}, worker)).finally(() => {
      controller.signal.removeEventListener('abort', cancel);
      if (controller.signal.aborted) return;

      if (current.signal.aborted) {
        for (const entry of selected) {
          if (entry.feedback.querySelector('.loading-state')) {
            entry.feedback.replaceChildren(
              el('p', {class: 'load-progress', role: 'status'},
                `Check cancelled for ${entry.instance.name}.${entry.body.childElementCount ? ' Previous result kept.' : ''}`)
            );
          }
        }
        statusAnnouncer.textContent = 'Fleet check was cancelled.';
      } else if (isFleetRound) {
        statusAnnouncer.textContent = 'Fleet connection check completed.';
      }

      content.querySelectorAll('.load-notice button').forEach(node => node.disabled = false);

      if (isFleetRound) {
        round = null;
        refresh.textContent = 'Check fleet';
      }
      updateRollup();
    });
  }

  start();
  // One fleet scan for the numbers on every card. A rate limit or an unreachable instance leaves dashes.
  if (entries.length) fleetMetrics({signal: controller.signal}).then(metrics => {
    for (const entry of entries) {
      const row = metrics?.data?.find(item => item.instance_id === entry.instance.id);
      entry.stats.replaceChildren(instanceStats(row || null));
    }
  }).catch(() => {});
  return () => {
    controller.abort();
    round?.abort();
  };
}
