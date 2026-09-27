import {api} from '../api.js';
import {state,pageSize,can} from '../state.js';
import {el,button,heading,table,empty} from '../lib/html.js';
import {icon,registerIcons} from '../lib/icons.js';
import {loadingState,loadFailure} from '../lib/payload.js';
import {instanceName,relativeLabel,relativePhrase,n8nUrl,repairHref} from '../lib/triage.js';
import {openPeek,peekFacts,peekSection,peekCopy,closePeek} from '../lib/peek.js';
import {rowMenu} from '../lib/row-menu.js';
import {segmented} from '../lib/select-menu.js';
import {cents,runCosts} from '../lib/run-costs.js';

registerIcons({
  'span-failed':'M12 3 2 20h20L12 3z M12 10v4 M12 17h.01',
  alert:'M12 9v4 M12 17h.01 M10.3 2.8 1.8 17.5A2 2 0 0 0 3.5 20.5h17a2 2 0 0 0 1.7-3L13.7 2.8a2 2 0 0 0-3.4 0z'
});

// Recorded nanosecond spans rendered as readable elapsed time; negative ranges are reported as unknown.
function elapsed(ns) {
  if (ns < 0n) return 'unknown';
  const value = Number(ns);
  if (value < 1e6) return `${(value / 1e3).toFixed(0)} µs`;
  if (value < 1e9) return `${(value / 1e6).toFixed(value < 1e7 ? 2 : 0)} ms`;
  return `${(value / 1e9).toFixed(value < 6e10 ? 2 : 1)} s`;
}

export async function render(container) {
  const controller = new AbortController();
  // Runs whose AI usage was already asked of n8n in this view; the server remembers finished runs too.
  const usageAsked = new Set();
  const instance = state.instanceId;
  const scope = state.instances.find(item => item.id === instance)?.name || 'all instances';

  let cursor = null;
  let busy = false;
  let loaded = false;
  let currentFilter = 'all'; // 'all' | 'errors'
  let tracesTable = null;
  let allRowsData = [];

  // Summary strip: dashes until the first successful read, "Unavailable" on failure, never 0.
  const tracesNum = el('p', {class: 'metric-number is-missing'}, '—');
  const spansNum = el('p', {class: 'metric-number is-missing'}, '—');
  const errorSpansNum = el('p', {class: 'metric-number is-missing'}, '—');
  const errorRateNum = el('p', {class: 'metric-number is-missing'}, '—');

  const summaryStrip = el('div', {class: 'observe-summary-strip', role: 'region', 'aria-label': 'Telemetry summary metrics'},
    el('div', {class: 'observe-metric-cell'}, el('h3', {}, 'Total traces'), tracesNum),
    el('div', {class: 'observe-metric-cell'}, el('h3', {}, 'Total steps'), spansNum),
    el('div', {class: 'observe-metric-cell metric-error'}, el('h3', {}, 'Error steps'), errorSpansNum),
    el('div', {class: 'observe-metric-cell'}, el('h3', {}, 'Error rate'), errorRateNum)
  );

  const messages = el('div');
  const list = el('div');
  const announcer = el('div', {class: 'sr-only', role: 'status'});

  // Filter & pagination controls
  const refresh = button('Refresh', () => load(), 'ghost');
  const next = button('Older traces', () => load(cursor), 'button ghost');
  next.hidden = true;

  // Two choices that switch often sit side by side, like the Errors status filter.
  const traceFilter = segmented({label: 'Trace filter', options: [
    {value: 'all', label: 'All traces', glyph: 'observe'},
    {value: 'errors', label: 'Errors only', glyph: 'errors'},
  ], value: 'all', onChange: (next) => setFilter(next)});

  // The filter stays out of the way until the first successful read finds traces.
  const toolbar = el('div', {class: 'observe-toolbar'}, traceFilter.node);
  toolbar.hidden = traceFilter.node.hidden = true;

  // Two tabs on one bar with the open tab's action on the right, like the AI workspace. AI cost comes
  // first: it reads n8n's own executions, so it works before any telemetry exporter is set up.
  const costs = runCosts({api, signal: controller.signal, instanceId: instance});
  const panels = {
    cost: el('section', {id: 'observe-cost'}, costs.node),
    traces: el('section', {id: 'observe-traces'}, summaryStrip, toolbar, messages,
      // Traces and their pagination share one card, like the error list.
      el('section', {class: 'card result-card'}, list, el('div', {class: 'result-card-foot'}, next)), announcer),
  };
  const tabs = {cost: button('AI cost', () => show('cost'), 'ghost'), traces: button('Traces', () => show('traces'), 'ghost')};
  const actions = el('div', {class: 'actions'});
  let costsLoaded = false;
  function show(tab) {
    for (const [name, panel] of Object.entries(panels)) {
      panel.hidden = name !== tab;
      tabs[name].setAttribute('aria-pressed', String(name === tab));
      tabs[name].setAttribute('aria-controls', panel.id);
    }
    actions.replaceChildren(tab === 'cost' ? costs.action : refresh);
    if (tab === 'cost' && !costsLoaded) {
      costsLoaded = true;
      costs.load();
    }
  }

  container.append(
    heading('Observability', 'Runs of your workflows, and what the AI steps cost.'),
    el('div', {class: 'studio-bar'}, el('nav', {class: 'studio-tabs', 'aria-label': 'Observability'}, tabs.cost, tabs.traces), actions),
    panels.cost,
    panels.traces
  );

  function setFilter(filter) {
    currentFilter = filter;
    traceFilter.value = filter;

    if (tracesTable) {
      const filtered = currentFilter === 'errors'
        ? allRowsData.filter(r => Number(r.error_spans || 0) > 0)
        : allRowsData;
      tracesTable.setRows(filtered.map(row => formatTableRow(row, metas.get(row.instance_id + ':' + row.trace_id))));
    }
  }

  // The trace list names each trace's workflow and execution, taken from whichever span reported
  // them, so labelling needs no extra requests. A trace no span named stays "Workflow unknown":
  // reading its spans one by one would find no name either.
  const metas = new Map();
  async function enrich(rows) {
    for (const row of rows) metas.set(row.instance_id + ':' + row.trace_id,
      {workflow: row.workflow_name || row.workflow_id || null, execution: row.execution_id || null});
  }

  // Span attributes and prices as label/value lines instead of a JSON dump; nested values stay compact.
  function factList(title, values) {
    const entries = Object.entries(values || {}).filter(([, value]) => value !== null && value !== undefined && value !== '');
    if (!entries.length) return null;
    return el('div', {class: 'span-facts'}, el('h4', {}, title),
      el('dl', {}, entries.map(([key, value]) => el('div', {}, el('dt', {}, key),
        el('dd', {}, typeof value === 'object' ? JSON.stringify(value) : String(value))))));
  }

  async function copyId(text, what) {
    try { await navigator.clipboard.writeText(text); announcer.textContent = `Copied ${what}.`; }
    catch { announcer.textContent = 'Copying is not available in this browser.'; }
  }

  // One control per row: the workflow name opens the waterfall beside the list.
  // Identifiers and the way back into n8n live behind the row's menu.
  function formatTableRow(row, meta) {
    const instName = instanceName(row.instance_id);
    const startMs = Number(BigInt(row.start_ns) / 1000000n);
    const startDate = new Date(startMs);
    const startEl = !Number.isNaN(startDate.getTime())
      ? el('time', {class: 'relative-time', datetime: startDate.toISOString(), title: startDate.toLocaleString()}, relativeLabel(startDate))
      : el('span', {class: 'relative-time muted'}, '—');

    const durNs = BigInt(row.end_ns) - BigInt(row.start_ns);
    const durFormatted = elapsed(durNs);
    const errorCount = Number(row.error_spans || 0);

    const healthBadge = errorCount > 0
      ? el('span', {class: 'badge error'}, `${errorCount} failed step${errorCount === 1 ? '' : 's'}`)
      : el('span', {class: 'badge success'}, 'No errors reported');

    const workflowName = meta?.workflow || 'Workflow unknown';
    // The name is the row's one inline action, styled as a title like Errors and Workflows (no pill, no glyph).
    // Several runs of one workflow share its name, so the name adds when the run started.
    const open = el('button', {type: 'button', class: 'workflow-open',
      'aria-label': `${workflowName}, started ${Number.isNaN(startDate.getTime()) ? 'at an unknown time' : relativePhrase(startDate)}`}, workflowName);
    open.querySelector(':scope > svg')?.remove();
    open.addEventListener('click', () => openTrace(row, meta));
    const execution = meta?.execution || null;
    const executionUrl = execution ? n8nUrl(row.instance_id, row.workflow_id, execution) : null;
    const workflowUrl = n8nUrl(row.instance_id, row.workflow_id);
    const menu = rowMenu([
      {label: 'Copy trace ID', onSelect: () => copyId(row.trace_id, 'trace ID')},
      execution ? {label: 'Copy execution ID', onSelect: () => copyId(execution, 'execution ID')} : null,
      executionUrl ? {label: 'Open execution in n8n', href: executionUrl, external: true}
        : workflowUrl ? {label: 'Open workflow in n8n', href: workflowUrl, external: true} : null,
    ], `More actions for ${workflowName}`);

    return [
      {sortValue: workflowName.toLowerCase(), content: open},
      {sortValue: instName, content: instName},
      {sortValue: startMs, content: startEl},
      {sortValue: Number(durNs), content: durFormatted},
      {sortValue: Number(row.span_count), content: String(row.span_count)},
      {sortValue: errorCount, content: healthBadge},
      {sortValue: '', content: menu},
    ];
  }

  function paintSummary(values) {
    // values is null when the request failed: Unavailable, never 0.
    const cells = [[tracesNum, values?.traces], [spansNum, values?.spans], [errorSpansNum, values?.errors], [errorRateNum, values?.rate]];
    for (const [node, value] of cells) {
      node.textContent = value === null || value === undefined ? 'Unavailable' : String(value);
      node.classList.toggle('is-missing', value === null || value === undefined);
    }
    errorSpansNum.classList.toggle('is-zero', !values || Number(values.errors) === 0);
  }

  async function loadSummary() {
    try {
      const params = new URLSearchParams();
      if (instance) params.set('instance_id', instance);
      const summaryData = await api('/api/otel/summary?' + params, {signal: controller.signal});
      if (controller.signal.aborted) return;

      const spans = Number(summaryData.spans || 0);
      const traces = Number(summaryData.traces || 0);
      const errorSpans = Number(summaryData.error_spans || 0);
      // A rate over no measured spans is undefined, never zero.
      const errorRate = spans > 0 ? ((errorSpans / spans) * 100).toFixed(1) + '%' : null;

      paintSummary({traces, spans, errors: errorSpans, rate: errorRate});
    } catch {
      // A failed summary reads as unavailable, never as zero, and never blocks the trace list.
      if (!controller.signal.aborted) paintSummary(null);
    }
  }

  // The waterfall for one trace, rendered beside the list: cost facts first, then
  // every step as a compact row with its detail on demand.
  function spanNodes(spans, start, end, withRuler) {
    const totalDuration = end > start ? end - start : 1n;
    const rulerTicks = el('div', {class: 'waterfall-ruler-ticks'},
      el('span', {}, '0 ms'),
      el('span', {}, elapsed(totalDuration / 4n)),
      el('span', {}, elapsed(totalDuration / 2n)),
      el('span', {}, elapsed((totalDuration * 3n) / 4n)),
      el('span', {}, elapsed(totalDuration))
    );
    const nodes = withRuler ? [el('div', {class: 'waterfall-ruler-wrap', 'aria-hidden': 'true'}, rulerTicks)] : [];
    for (const span of spans) {
      const spanStart = BigInt(span.start_ns);
      const spanEnd = BigInt(span.end_ns);
      const spanDuration = spanEnd - spanStart;
      const offset = Number((spanStart - start) * 10000n / totalDuration) / 100;
      const width = Math.max(0.3, Number(spanDuration * 10000n / totalDuration) / 100);
      const status = span.health_status || span.status || 'Unknown';
      const failed = span.status === 'error' || span.health_status === 'error';
      // Steps read as their node name; the raw span name (workflow.execute…) is only a fallback.
      const nodeLabel = span.node_name || span.name || 'Step';
      const description = `${nodeLabel}: ${failed ? 'failed, ' : ''}starts at ${elapsed(spanStart - start)}, duration ${elapsed(spanDuration)}, status ${status}`;
      // Only AI steps carry a price: other nodes show no cost at all, never "Unknown".
      const ai = span.model != null || span.tokens_in != null || span.tokens_out != null;
      const bar = el('div', {className: failed ? 'waterfall-bar failed' : 'waterfall-bar',
        role: 'img', 'aria-label': description, title: description});
      bar.style.marginLeft = `${Math.max(0, Math.min(100, offset))}%`;
      bar.style.width = `${Math.max(0.3, Math.min(100 - offset, width))}%`;
      const spanActions = el('div', {class: 'span-actions'});
      if (span.workflow_id) {
        spanActions.append(
          el('a', {class: 'button ghost', href: '#workflows?' + new URLSearchParams({instance_id: span.instance_id, workflow_id: span.workflow_id})}, 'Workflows'),
          can('operator') ? el('a', {class: 'button ghost', href: repairHref({instance_id: span.instance_id, workflow_id: span.workflow_id})}, 'Repair with AI') : null
        );
      }
      if (failed) {
        spanActions.append(el('a', {class: 'button ghost', href: '#errors'}, 'View errors'));
      }
      nodes.push(el('details', {class: 'waterfall-span'},
        el('summary', {class: 'waterfall-row'},
          el('span', {class: 'waterfall-name'},
            failed ? el('span', {className: 'span-failed'}, icon('span-failed'), 'Failed') : null,
            el('span', {class: 'waterfall-node'}, nodeLabel),
            ai ? el('span', {class: 'waterfall-cost'}, cents(span.cost_exact)) : null),
          el('span', {class: 'waterfall-track'}, bar)),
        failed && (span.error_summary || span.error_type)
          ? el('div', {class: 'instance-diagnostic-box is-unreachable'}, icon('alert'),
              el('div', {}, el('strong', {}, `${span.error_type || 'ExecutionError'}: `),
                span.error_summary || 'Span execution failed.')) : null,
        el('p', {className: 'muted'}, `Status: ${status} · Duration: ${elapsed(spanDuration)}` +
          (ai ? ` · Model: ${span.model || 'Unreported'} · Input tokens: ${span.tokens_in ?? 'Unknown'} · Output tokens: ${span.tokens_out ?? 'Unknown'}` : '')),
        ai ? factList('Price', span.pricing) : null,
        factList('Attributes', span.attributes),
        spanActions.childElementCount ? spanActions : null));
    }
    return nodes;
  }

  // The root span names the whole run, so it reads as the panel's header bar;
  // only its child steps expand below. A trace with no single root keeps every span as a step.
  function rootHeader(root) {
    const duration = elapsed(BigInt(root.end_ns) - BigInt(root.start_ns));
    const raw = root.health_status || root.status || '';
    const status = {error: 'Failed', ok: 'Succeeded', unset: 'No status reported'}[raw] || raw || 'Status unknown';
    const reason = raw === 'error' ? root.error_summary || root.error_type || null : null;
    return el('div', {class: 'waterfall-root', title: `Whole run · ${duration} · ${status}`},
      el('span', {class: 'waterfall-node'}, 'Whole run'),
      el('span', {class: 'waterfall-meta'}, `${duration} · ${status}`),
      reason ? el('span', {class: 'waterfall-root-reason'}, reason) : null);
  }

  function openTrace(row, meta) {
    const workflow = meta?.workflow || 'Workflow unknown';
    const execution = meta?.execution || null;
    const startMs = Number(BigInt(row.start_ns) / 1000000n);
    const startDate = new Date(startMs);
    const started = Number.isNaN(startDate.getTime()) ? null : startDate;
    const executionUrl = execution ? n8nUrl(row.instance_id, row.workflow_id, execution) : null;
    const workflowUrl = n8nUrl(row.instance_id, row.workflow_id);
    const running = el('div', {class: 'stack', role: 'status'}, loadingState(`Loading steps for ${workflow}…`));
    const steps = el('div', {class: 'stack'});
    const dialog = openPeek({
      eyebrow: instanceName(row.instance_id),
      title: workflow,
      meta: started ? `Started ${started.toLocaleString()}` : 'Start time unknown',
      sections: [
        peekFacts([
          ['Trace ID', peekCopy(row.trace_id, 'trace ID')],
          ['Execution', execution ? peekCopy(execution, 'execution ID') : 'Not recorded'],
        ]),
        running,
        peekSection('Steps', steps),
      ],
      actions: [
        executionUrl ? el('a', {class: 'button', href: executionUrl, target: '_blank', rel: 'noopener noreferrer'}, 'Open execution in n8n')
          : workflowUrl ? el('a', {class: 'button', href: workflowUrl, target: '_blank', rel: 'noopener noreferrer'}, 'Open workflow in n8n') : null,
        row.workflow_id && can('operator') ? el('a', {class: 'button ghost', href: repairHref({instance_id: row.instance_id, workflow_id: row.workflow_id})}, 'Repair with AI') : null,
      ],
    });
    const load = async (after = null) => {
      const params = new URLSearchParams({instance_id: row.instance_id, limit: String(pageSize())});
      if (after) params.set('after', String(after));
      try {
        const result = await api('/api/otel/traces/' + encodeURIComponent(row.trace_id) + '?' + params, {signal: controller.signal});
        if (controller.signal.aborted || !dialog.isConnected) return;
        if (!after) {
          // The server totals every AI step of the run, not just this page. No AI steps: no cost line at all.
          const aiSteps = Number(result.ai_spans || 0), unpriced = Number(result.unknown_cost_spans || 0);
          const known = Number(result.known_cost_usd || 0) > 0;
          const unpricedText = `${unpriced} AI ${unpriced === 1 ? 'step' : 'steps'} without a price`;
          if (!aiSteps) running.replaceChildren(...(usageNote ? [el('p', {className: 'muted'}, usageNote)] : []));
          else running.replaceChildren(el('div', {class: 'trace-cost'},
            peekFacts([
              ['AI cost', !unpriced ? cents(result.known_cost_usd) : known ? `At least ${cents(result.known_cost_usd)} · ${unpricedText}` : `Unknown · ${unpricedText}`],
            ]),
            el('p', {className: 'muted'}, 'All AI steps of this run. Other steps carry no price.'),
            unpriced ? el('p', {className: 'muted'}, can('admin')
              ? ['A model without a price needs one: ', el('a', {href: '#settings'}, 'set prices in Settings › AI prices'), '.']
              : 'A model without a price needs one from an administrator.') : null,
            usageNote ? el('p', {className: 'muted'}, usageNote) : null));
        }
        const roots = !after ? result.data.filter(span => !span.parent_id) : [];
        const root = roots.length === 1 ? roots[0] : null;
        const shown = root ? result.data.filter(span => span !== root) : result.data;
        if (!result.data.length) {
          if (!after) steps.append(el('p', {className: 'muted'}, 'No steps on this page.'));
        } else {
          if (root) steps.append(rootHeader(root));
          steps.append(...spanNodes(shown, BigInt(row.start_ns), BigInt(row.end_ns), !after));
        }
        if (result.next_cursor) {
          const more = button('Next steps', () => { more.disabled = true; load(result.next_cursor).finally(() => more.remove()); }, 'ghost');
          steps.append(more);
        }
      } catch (error) {
        if (!controller.signal.aborted && dialog.isConnected)
          running.replaceChildren(loadFailure(error, () => load(after), `Could not load steps for ${workflow}`));
      }
    };
    // n8n's spans usually carry no token counts; its execution records them per model node. Ask the server
    // to read them once for this run (it keeps what it read), then show the steps.
    let usageNote = null;
    (async () => {
      const key = `${row.instance_id}:${row.trace_id}`;
      if (execution && !usageAsked.has(key)) {
        usageAsked.add(key);
        running.replaceChildren(el('p', {class: 'load-progress', role: 'status'}, 'Reading AI usage from n8n…'));
        try {
          const read = await api('/api/otel/traces/' + encodeURIComponent(row.trace_id) + '/usage?' + new URLSearchParams({instance_id: row.instance_id}),
            {method: 'POST', signal: controller.signal});
          // n8n prunes old executions, and many setups never save successful ones.
          if (read.missing) usageNote = 'n8n did not keep this execution, so AI token counts come from telemetry only.';
        } catch (error) {
          if (error.name === 'AbortError' || controller.signal.aborted) return;
          usageAsked.delete(key);  // Opening the run again tries again.
          const reason = String(error.message || 'The request failed').replace(/[.\s]+$/, '');
          usageNote = `Could not read AI usage from n8n: ${reason}.`;
        }
      }
      if (!controller.signal.aborted && dialog.isConnected) await load();
    })();
  }

  async function load(before = null) {
    if (busy || controller.signal.aborted) return;
    busy = true;
    next.disabled = refresh.disabled = true;

    messages.replaceChildren(loaded
      ? el('p', {class: 'load-progress', role: 'status'}, `Refreshing traces for ${scope}…`)
      : loadingState(`Loading traces for ${scope}…`)
    );

    try {
      const params = new URLSearchParams({limit: String(pageSize())});
      if (instance) params.set('instance_id', instance);
      if (before) params.set('before', String(before));

      const [result] = await Promise.all([
        api('/api/otel/traces?' + params, {signal: controller.signal}),
        loadSummary()
      ]);

      if (controller.signal.aborted) return;

      cursor = result.next_cursor;
      loaded = true;
      messages.replaceChildren();

      if (before && tracesTable) {
        // Appending older traces to existing table
        allRowsData.push(...result.data);
        await enrich(result.data);
        if (controller.signal.aborted) return;
        const filteredNewRows = currentFilter === 'errors'
          ? result.data.filter(r => Number(r.error_spans || 0) > 0)
          : result.data;
        tracesTable.appendRows(filteredNewRows.map(row => formatTableRow(row, metas.get(row.instance_id + ':' + row.trace_id))));
        toolbar.hidden = traceFilter.node.hidden = !allRowsData.length;
      } else {
        // Initial or refreshed traces table
        allRowsData = result.data;
        await enrich(result.data);
        if (controller.signal.aborted) return;
        toolbar.hidden = traceFilter.node.hidden = !allRowsData.length;
        if (result.data.length) {
          const displayRows = currentFilter === 'errors'
            ? allRowsData.filter(r => Number(r.error_spans || 0) > 0)
            : allRowsData;

          tracesTable = table(['Workflow', 'Instance', 'Started', 'Duration', 'Steps', 'Health', ''], displayRows.map(row => formatTableRow(row, metas.get(row.instance_id + ':' + row.trace_id))), {
            numeric: [4],
            sortable: {0: 'text', 1: 'text', 2: 'number', 3: 'number', 4: 'number', 5: 'number'},
            initialSort: {column: 2, direction: 'desc'}
          });
          list.replaceChildren(tracesTable);
        } else {
          tracesTable = null;
          list.replaceChildren(empty('Waiting for telemetry', 'Create an exporter token in Instances and connect your n8n telemetry exporter.', [el('a', {class: 'button ghost', href: '#instances?telemetry=' + encodeURIComponent(instance || '')}, 'Set up telemetry')], 'observe'));
        }
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        messages.replaceChildren(loadFailure(error, () => load(before), `Could not load traces for ${scope}`));
      }
    } finally {
      busy = false;
      next.disabled = false;
      next.hidden = !cursor;
      refresh.disabled = false;
    }
  }

  show(new URLSearchParams(location.hash.split('?')[1] || '').get('tab') === 'traces' ? 'traces' : 'cost');
  load();
  return () => {
    controller.abort();
    closePeek();
  };
}
