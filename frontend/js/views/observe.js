import {api} from '../api.js';
import {state,pageSize} from '../state.js';
import {el,button,heading,table,empty,jsonView} from '../lib/html.js';
import {icon,registerIcons} from '../lib/icons.js';
import {loadingState,loadFailure,asOf} from '../lib/payload.js';
import {instanceName,relativeLabel} from '../lib/triage.js';

registerIcons({
  'span-failed':'M12 3 2 20h20L12 3z M12 10v4 M12 17h.01',
  alert:'M12 9v4 M12 17h.01 M10.3 2.8 1.8 17.5A2 2 0 0 0 3.5 20.5h17a2 2 0 0 0 1.7-3L13.7 2.8a2 2 0 0 0-3.4 0z'
});

// Decimal-point shifting preserves every digit of server-priced costs.
export function cents(value) {
  if (value === null || value === undefined) return 'Unknown';
  const match = String(value).match(/^([+-]?)(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/);
  if (!match) return String(value) + ' USD';
  const digits = match[2] + (match[3] || ''), point = match[2].length + Number(match[4] || 0) + 2;
  if (Math.abs(point) > 1000) return String(value) + ' USD';
  const shifted = point <= 0 ? '0.' + '0'.repeat(-point) + digits : point >= digits.length ? digits + '0'.repeat(point - digits.length) : digits.slice(0, point) + '.' + digits.slice(point);
  return match[1] + shifted.replace(/^0+(?=\d)/, '') + ' ¢';
}

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
  const instance = state.instanceId;
  const scope = state.instances.find(item => item.id === instance)?.name || 'all instances';

  let cursor = null;
  let busy = false;
  let loaded = false;
  let inspection = null;
  let currentFilter = 'all'; // 'all' | 'errors'
  let tracesTable = null;
  let allRowsData = [];

  // Summary strip
  const tracesNum = el('p', {class: 'metric-number'}, '0');
  const spansNum = el('p', {class: 'metric-number'}, '0');
  const errorSpansNum = el('p', {class: 'metric-number'}, '0');
  const errorRateNum = el('p', {class: 'metric-number'}, '0%');

  const summaryStrip = el('div', {class: 'observe-summary-strip', role: 'region', 'aria-label': 'Telemetry summary metrics'},
    el('div', {class: 'observe-metric-cell'}, el('h3', {}, 'Total traces'), tracesNum),
    el('div', {class: 'observe-metric-cell'}, el('h3', {}, 'Total spans'), spansNum),
    el('div', {class: 'observe-metric-cell metric-error'}, el('h3', {}, 'Error spans'), errorSpansNum),
    el('div', {class: 'observe-metric-cell'}, el('h3', {}, 'Error rate'), errorRateNum)
  );

  const messages = el('div');
  const stamp = el('div');
  const list = el('div');

  const detail = el('section', {className: 'card stack observe-waterfall-panel', hidden: true, 'aria-label': 'Trace waterfall inspection'});
  const detailMessages = el('div');
  const detailStamp = el('div');
  const detailContent = el('div', {class: 'stack'});
  detail.append(detailMessages, detailStamp, detailContent);

  // Filter & pagination controls
  const refresh = button('Refresh', () => load(), 'ghost');
  const next = button('Older traces', () => load(cursor), 'button ghost');

  const filterAllBtn = button('All traces', () => setFilter('all'), 'ghost observe-filter-btn', {'aria-pressed': 'true'});
  const filterErrorsBtn = button('Errors only', () => setFilter('errors'), 'ghost observe-filter-btn', {'aria-pressed': 'false'});
  const filterGroup = el('div', {class: 'observe-filter-group'}, filterAllBtn, filterErrorsBtn);

  const toolbar = el('div', {class: 'observe-toolbar'}, filterGroup, refresh);

  container.append(
    heading('Observability', 'OpenTelemetry trace waterfalls and exact recorded AI costs.'),
    summaryStrip,
    toolbar,
    messages,
    // Traces and their pagination share one card, like the error list.
    el('section', {class: 'card result-card'}, list, el('div', {class: 'result-card-foot'}, next)),
    detail
  );

  function setFilter(filter) {
    currentFilter = filter;
    filterAllBtn.setAttribute('aria-pressed', String(filter === 'all'));
    filterErrorsBtn.setAttribute('aria-pressed', String(filter === 'errors'));

    if (tracesTable) {
      const filtered = currentFilter === 'errors'
        ? allRowsData.filter(r => Number(r.error_spans || 0) > 0)
        : allRowsData;
      tracesTable.setRows(filtered.map(formatTableRow));
    }
  }

  function formatTableRow(row) {
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
      ? el('span', {class: 'badge error'}, `${errorCount} error${errorCount === 1 ? '' : 's'}`)
      : el('span', {class: 'badge success'}, 'Healthy');

    const traceDisplay = el('span', {class: 'mono', title: row.trace_id},
      row.trace_id.length > 16 ? row.trace_id.slice(0, 16) + '…' : row.trace_id
    );

    return [
      {sortValue: row.trace_id, content: traceDisplay},
      {sortValue: instName, content: instName},
      {sortValue: startMs, content: startEl},
      {sortValue: Number(durNs), content: durFormatted},
      {sortValue: Number(row.span_count), content: String(row.span_count)},
      {sortValue: errorCount, content: healthBadge},
      button('View waterfall', () => inspect(row), 'button ghost', {'aria-label': `View waterfall for trace ${row.trace_id}`})
    ];
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
      const errorRate = spans > 0 ? ((errorSpans / spans) * 100).toFixed(1) + '%' : '0%';

      tracesNum.textContent = String(traces);
      spansNum.textContent = String(spans);
      errorSpansNum.textContent = String(errorSpans);
      errorRateNum.textContent = errorRate;
    } catch {
      // Summary error should not block trace list
    }
  }

  let returnFocusEl = null;
  let returnFallbackEl = null;

  function closeInspection() {
    detail.hidden = true;
    inspection?.abort();
    if (returnFocusEl?.isConnected) {
      returnFocusEl.focus();
    } else if (returnFallbackEl?.isConnected) {
      if (!returnFallbackEl.hasAttribute('tabindex')) returnFallbackEl.setAttribute('tabindex', '-1');
      returnFallbackEl.focus();
    }
  }

  detail.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !detail.hidden) {
      event.stopPropagation();
      closeInspection();
    }
  });

  async function inspect(row, after = null) {
    inspection?.abort();
    const current = new AbortController();
    inspection = current;

    const active = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (!after && active) {
      returnFocusEl = active;
      returnFallbackEl = active.closest('main, [role="main"], #app, .view-content, .content, section') || active.parentElement || list || null;
    }

    detail.hidden = false;
    const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches;
    detail.scrollIntoView({behavior: reducedMotion ? 'auto' : 'smooth', block: 'nearest'});

    detailMessages.replaceChildren(loadingState(`Loading trace ${row.trace_id}…`));
    try {
      const params = new URLSearchParams({instance_id: row.instance_id, limit: String(pageSize())});
      if (after) params.set('after', String(after));

      const result = await api('/api/otel/traces/' + encodeURIComponent(row.trace_id) + '?' + params, {signal: current.signal});
      if (current.signal.aborted || controller.signal.aborted) return;

      detailMessages.replaceChildren();
      detailStamp.replaceChildren(asOf());

      const closeBtn = button('Close inspection', closeInspection, 'ghost');
      const panelHeading = el('h2', {tabindex: '-1'}, `Trace ${row.trace_id}`);

      const headerRow = el('div', {class: 'observe-waterfall-head'},
        el('div', {class: 'observe-waterfall-title'},
          panelHeading,
          el('p', {class: 'muted'}, `Instance: ${instanceName(row.instance_id)} · ${result.data.length} span(s) on this page`)
        ),
        closeBtn
      );

      const aiCostStrip = el('div', {class: 'observe-ai-strip'},
        el('div', {class: 'observe-ai-stat'},
          el('span', {class: 'stat-label'}, 'Known AI Cost'),
          el('span', {class: 'stat-value'}, cents(result.known_cost_usd))
        ),
        el('div', {class: 'observe-ai-stat'},
          el('span', {class: 'stat-label'}, 'Total Calculated Cost'),
          el('span', {class: 'stat-value'}, cents(result.cost_usd))
        ),
        el('div', {class: 'observe-ai-stat'},
          el('span', {class: 'stat-label'}, 'Unpriced Spans'),
          el('span', {class: 'stat-value'}, `${result.unknown_cost_spans} span(s)`)
        )
      );

      const provenanceNote = el('p', {className: 'muted'}, 'Costs and tokens below cover this returned page; estimates retain their original pricing provenance.');

      detailContent.replaceChildren(headerRow, aiCostStrip, provenanceNote);
      if (!after) {
        panelHeading.focus();
      }

      if (!result.data.length) {
        detailContent.append(empty('No further spans', 'This trace has no spans on this page.'));
        return;
      }

      const start = result.data.reduce((min, span) => BigInt(span.start_ns) < min ? BigInt(span.start_ns) : min, BigInt(result.data[0].start_ns));
      const end = result.data.reduce((max, span) => BigInt(span.end_ns) > max ? BigInt(span.end_ns) : max, start + 1n);
      const totalDuration = end - start || 1n;

      // Time scale ruler
      const rulerTicks = el('div', {class: 'waterfall-ruler-ticks'},
        el('span', {}, '0 ms'),
        el('span', {}, elapsed(totalDuration / 4n)),
        el('span', {}, elapsed(totalDuration / 2n)),
        el('span', {}, elapsed((totalDuration * 3n) / 4n)),
        el('span', {}, elapsed(totalDuration))
      );
      const rulerWrap = el('div', {class: 'waterfall-ruler-wrap', 'aria-hidden': 'true'}, rulerTicks);
      detailContent.append(rulerWrap);

      for (const span of result.data) {
        const spanStart = BigInt(span.start_ns);
        const spanEnd = BigInt(span.end_ns);
        const spanDuration = spanEnd - spanStart;

        const offset = Number((spanStart - start) * 10000n / totalDuration) / 100;
        const width = Math.max(0.3, Number(spanDuration * 10000n / totalDuration) / 100);

        const status = span.health_status || span.status || 'Unknown';
        const failed = span.status === 'error' || span.health_status === 'error';
        const description = `${span.name}: ${failed ? 'failed, ' : ''}starts at ${elapsed(spanStart - start)}, duration ${elapsed(spanDuration)}, status ${status}`;

        const bar = el('div', {
          className: failed ? 'waterfall-bar failed' : 'waterfall-bar',
          role: 'img',
          'aria-label': description,
          title: description
        });
        bar.style.marginLeft = `${Math.max(0, Math.min(100, offset))}%`;
        bar.style.width = `${Math.max(0.3, Math.min(100 - offset, width))}%`;

        // Diagnostic / repair actions for spans
        const spanActions = el('div', {class: 'span-actions'});
        if (span.workflow_id) {
          spanActions.append(
            el('a', {class: 'button ghost', href: '#workflows'}, 'Workflows'),
            el('a', {class: 'button ghost', href: `#assistant?mode=repair&instance_id=${encodeURIComponent(span.instance_id)}&workflow_id=${encodeURIComponent(span.workflow_id)}`}, 'Repair with AI')
          );
        }
        if (failed) {
          spanActions.append(el('a', {class: 'button ghost', href: `#errors?instance_id=${encodeURIComponent(span.instance_id)}`}, 'View Errors'));
        }

        const summaryNode = el('summary', {},
          failed ? el('span', {className: 'span-failed'}, icon('span-failed'), 'Failed') : null,
          `${span.name} · ${status} · ${cents(span.cost_exact)}`
        );

        const errorGuidance = failed && (span.error_summary || span.error_type)
          ? el('div', {class: 'instance-diagnostic-box is-unreachable'},
              icon('alert'),
              el('div', {},
                el('strong', {}, `${span.error_type || 'ExecutionError'}: `),
                span.error_summary || 'Span execution failed.'
              )
            )
          : null;

        const info = el('details', {},
          summaryNode,
          errorGuidance,
          el('p', {className: 'muted'}, `Model: ${span.model || 'Unreported'} · Input tokens: ${span.tokens_in ?? 'Unknown'} · Output tokens: ${span.tokens_out ?? 'Unknown'}`),
          jsonView({pricing: span.pricing, attributes: span.attributes, parent_id: span.parent_id}),
          spanActions.childElementCount ? spanActions : null
        );

        detailContent.append(el('div', {className: failed ? 'stack waterfall-span failed' : 'stack waterfall-span'}, info, el('div', {className: 'waterfall-track'}, bar)));
      }

      if (result.next_cursor) {
        detailContent.append(button('Next span page', () => inspect(row, result.next_cursor), 'button ghost'));
      }
    } catch (error) {
      if (!controller.signal.aborted && !current.signal.aborted) {
        detailMessages.replaceChildren(loadFailure(error, () => inspect(row, after), `Could not load trace ${row.trace_id}`));
      }
    }
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
      stamp.replaceChildren(asOf());

      if (before && tracesTable) {
        // Appending older traces to existing table
        allRowsData.push(...result.data);
        const filteredNewRows = currentFilter === 'errors'
          ? result.data.filter(r => Number(r.error_spans || 0) > 0)
          : result.data;
        tracesTable.appendRows(filteredNewRows.map(formatTableRow));
      } else {
        // Initial or refreshed traces table
        allRowsData = result.data;
        if (result.data.length) {
          const displayRows = currentFilter === 'errors'
            ? allRowsData.filter(r => Number(r.error_spans || 0) > 0)
            : allRowsData;

          tracesTable = table(['Trace', 'Instance', 'Started', 'Duration', 'Spans', 'Health', ''], displayRows.map(formatTableRow), {
            numeric: [4],
            sortable: {0: 'text', 1: 'text', 2: 'number', 3: 'number', 4: 'number', 5: 'number'},
            initialSort: {column: 2, direction: 'desc'}
          });
          list.replaceChildren(tracesTable);
        } else {
          tracesTable = null;
          list.replaceChildren(empty('Waiting for telemetry', 'Create an exporter token in Instances and connect your n8n telemetry exporter.', [el('a', {class: 'button ghost', href: '#instances?telemetry=' + encodeURIComponent(instance || '')}, 'Set up telemetry')]));
        }
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        messages.replaceChildren(loadFailure(error, () => load(before), `Could not load traces for ${scope}`));
      }
    } finally {
      busy = false;
      next.disabled = !cursor;
      refresh.disabled = false;
    }
  }

  load();
  return () => {
    controller.abort();
    inspection?.abort();
  };
}
