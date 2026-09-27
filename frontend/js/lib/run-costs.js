// AI cost of n8n runs, read straight from their executions, so it works without a telemetry exporter.
// The server reads each finished run once and keeps only its AI calls' counts, models and prices.
import {el,button,table,empty} from './html.js';
import {icon} from './icons.js';
import {loadingState,loadFailure} from './payload.js';
import {instanceName,relativeTime,relativePhrase,parseTimestamp,runBadge,n8nUrl} from './triage.js';
import {openPeek,peekFacts,peekSection,peekCopy} from './peek.js';
import {rowMenu} from './row-menu.js';
import {state,pageSize,can} from '../state.js';

// A USD amount (a decimal string) for reading, always in dollars: cents from $1, four decimal places below it
// (one AI call often costs a fraction of a cent), and two significant digits under $0.0001 so a real cost
// never reads as $0. The exact amount stays in the data.
export function dollars(value) {
  if (value === null || value === undefined) return 'Unknown';
  const usd = Number(value);
  if (!Number.isFinite(usd)) return `${value} USD`;
  if (usd === 0) return '$0';
  const size = Math.abs(usd);
  const text = size >= 1 ? usd.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})
    : size >= 0.0001 ? usd.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 4})
    : usd.toLocaleString('en-US', {maximumSignificantDigits: 2});
  return `$${text}`;
}

const DAYS = 7;
const SERVICES = {openai: 'OpenAI', anthropic: 'Anthropic', google: 'Google Gemini', vertex: 'Google Vertex AI', openrouter: 'OpenRouter',
  groq: 'Groq', mistral: 'Mistral', deepseek: 'DeepSeek', xai: 'xAI', azure: 'Azure OpenAI', ollama: 'Ollama', bedrock: 'Amazon Bedrock',
  together: 'Together AI', fireworks: 'Fireworks', perplexity: 'Perplexity', cohere: 'Cohere', moonshotai: 'Moonshot AI', minimax: 'MiniMax',
  alibaba: 'Alibaba Cloud', vercel: 'Vercel AI Gateway', 'openai-compatible': 'Other AI service'};
const SOURCES = {override: 'your price', openrouter: 'OpenRouter catalog', bundled: 'bundled price', ollama: 'local Ollama, no charge'};
const count = value => Number(value || 0).toLocaleString('en-US');
const plural = (value, one, many = `${one}s`) => `${count(value)} ${Number(value) === 1 ? one : many}`;

// A cost that may be partly unknown: the whole amount, "At least" what is priced, or Unknown.
function costText({cost_usd: cost, known_cost_usd: known, unpriced_calls: unpriced, ai_calls: calls}) {
  if (!Number(calls)) return '—';
  if (cost !== null && cost !== undefined) return dollars(cost);
  return Number(known) > 0 ? `At least ${dollars(known)}` : 'No price yet';
}

export function runCosts({api, signal, instanceId}) {
  const scope = instanceId ? [instanceId] : state.instances.map(item => item.id);
  const scopeName = instanceId ? instanceName(instanceId) : 'all instances';
  const cell = (title, hint) => {
    const value = el('p', {class: 'metric-number is-missing'}, '—');
    const note = el('p', {class: 'metric-hint'}, hint || '');
    return {value, note, node: el('div', {class: 'observe-metric-cell'}, el('h3', {}, title), value, note)};
  };
  const costCell = cell('AI cost', `Last ${DAYS} days`), runsCell = cell('Runs with AI'), callsCell = cell('AI calls'), unpricedCell = cell('Without a price');
  const strip = el('div', {class: 'observe-summary-strip', role: 'region', 'aria-label': `AI cost, last ${DAYS} days`},
    costCell.node, runsCell.node, callsCell.node, unpricedCell.node);
  const notes = el('div', {class: 'run-cost-notes'});
  const messages = el('div');
  const byWorkflow = el('div');
  const runs = el('div');
  const olderRuns = button('Older runs', () => loadRuns(listCursor), 'ghost');
  const checkOlder = button('Check older runs in n8n', () => check(true), 'ghost');
  olderRuns.hidden = checkOlder.hidden = true;
  const action = button('Check for new runs', () => check(false), 'ghost');
  const node = el('div', {class: 'stack run-costs'}, strip, messages, notes,
    el('section', {class: 'card result-card', 'aria-label': 'AI cost by workflow', hidden: true}, byWorkflow),
    el('section', {class: 'card result-card', 'aria-label': 'Runs with AI calls', hidden: true}, runs,
      el('div', {class: 'result-card-foot'}, olderRuns, checkOlder)));
  let listCursor = null, busy = false, runsTable = null;
  const scanCursors = new Map();

  function paintStrip(summary) {
    const set = ({value}, text, missing = false) => { value.textContent = text; value.classList.toggle('is-missing', missing); };
    if (!summary) {
      for (const item of [costCell, runsCell, callsCell, unpricedCell]) set(item, 'Unavailable', true);
      return;
    }
    set(costCell, summary.ai_calls ? costText(summary) : '$0');
    set(runsCell, count(summary.ai_runs));
    runsCell.note.textContent = `of ${plural(summary.runs, 'run')} checked`;
    set(callsCell, count(summary.ai_calls));
    set(unpricedCell, count(summary.unpriced_calls));
    unpricedCell.note.replaceChildren(summary.unpriced_calls && can('admin') ? el('a', {href: '#settings'}, 'Set prices in Settings') : '');
  }

  function note(...children) {
    notes.append(el('p', {class: 'coverage-partial', role: 'status'}, icon('info'), el('span', {}, ...children)));
  }

  function paintNotes(results, summary) {
    notes.replaceChildren();
    for (const {instance, error, result} of results) {
      if (error?.code === 'rate_limited' || error?.status === 429) note(`${instanceName(instance)} was checked moments ago, so these are the runs on record.`);
      else if (error) note(`Could not check ${instanceName(instance)} for new runs: ${String(error.message || error.detail || 'the request failed').replace(/[.\s]+$/, '')}. The costs below cover the runs read before.`);
      else if (result?.error) note(`Checking ${instanceName(instance)} stopped after ${plural(result.read, 'run')}: ${String(result.error.detail || 'n8n did not answer').replace(/[.\s]+$/, '')}. Check for new runs to continue.`);
      else if (result && !result.complete && !result.next_cursor && result.read) note(`Checking ${instanceName(instance)} took too long and stopped after ${plural(result.read, 'run')}. Check for new runs to continue.`);
      if (result?.running) note(`${plural(result.running, 'run')} on ${instanceName(instance)} ${result.running === 1 ? 'is' : 'are'} still going; ${result.running === 1 ? 'its' : 'their'} cost appears once ${result.running === 1 ? 'it finishes' : 'they finish'}.`);
    }
    if (summary?.unreadable_runs) note(`${plural(summary.unreadable_runs, 'run')} could not be read from n8n (it no longer has them, or their data is too large), so ${summary.unreadable_runs === 1 ? 'its' : 'their'} AI cost is unknown.`);
    if (summary?.truncated) note(`Only the newest ${count(summary.runs)} runs are counted in the totals.`);
    checkOlder.hidden = ![...scanCursors.values()].some(Boolean);
  }

  function paintWorkflows(summary) {
    const rows = summary?.by_workflow || [];
    if (!rows.length) { byWorkflow.closest('section').hidden = true; return; }
    byWorkflow.closest('section').hidden = false;
    byWorkflow.replaceChildren(el('h2', {class: 'result-card-title'}, `By workflow, last ${DAYS} days`),
      table(['Workflow', 'Instance', 'Runs with AI', 'AI calls', 'AI cost'], rows.map(row => [
        row.workflow_name || row.workflow_id || 'Workflow unknown', instanceName(row.instance_id),
        {sortValue: row.ai_runs, content: count(row.ai_runs)}, {sortValue: row.ai_calls, content: count(row.ai_calls)},
        {sortValue: Number(row.known_cost_usd), content: costText(row)},
      ]), {numeric: [2, 3, 4], sortable: {0: 'text', 1: 'text', 2: 'number', 3: 'number', 4: 'number'}, initialSort: {column: 4, direction: 'desc'}}));
  }

  // One block per call: a side panel is too narrow for a six-column table.
  function callList(calls) {
    return el('ul', {class: 'run-calls'}, calls.map(call => {
      const priced = call.cost_usd !== null && call.cost_usd !== undefined;
      const source = priced ? SOURCES[call.pricing?.pricing_source] || call.pricing?.pricing_source || '' : 'no price for this model';
      return el('li', {},
        el('div', {class: 'run-call-head'}, el('strong', {}, call.node), el('span', {class: 'run-call-cost'}, priced ? dollars(call.cost_usd) : 'No price')),
        el('p', {}, `${SERVICES[call.provider] || call.provider || 'Unknown service'} · `, el('code', {}, call.model || 'model not reported')),
        el('p', {class: 'muted'}, `${count(call.tokens_in)} in${call.cached_tokens ? ` (${count(call.cached_tokens)} cached)` : ''} · ${count(call.tokens_out)} out`
          + `${call.reasoning_tokens ? ` (${count(call.reasoning_tokens)} reasoning)` : ''} · ${source}${priced && call.pricing?.estimated ? ', estimate' : ''}`));
    }));
  }

  function openRun(run) {
    const workflow = run.workflow_name || run.workflow_id || 'Workflow unknown';
    const started = parseTimestamp(run.started_at || '');
    const url = n8nUrl(run.instance_id, run.workflow_id, run.execution_id);
    openPeek({
      eyebrow: instanceName(run.instance_id), title: workflow,
      meta: started ? `Started ${started.toLocaleString()}` : 'Start time unknown',
      sections: [
        peekFacts([['Execution', peekCopy(run.execution_id, 'execution ID')], ['Status', runBadge(run.status)], ['AI cost', costText(run)]]),
        peekSection('AI calls', callList(run.calls || []),
          el('p', {class: 'muted'}, 'Token counts come from n8n\'s AI nodes, or from the AI service\'s own answer to an HTTP Request node. Costs use the price that applied when the run was read.')),
      ],
      actions: [url ? el('a', {class: 'button', href: url, target: '_blank', rel: 'noopener noreferrer'}, 'Open execution in n8n') : null],
    });
  }

  function runRow(run) {
    const workflow = run.workflow_name || run.workflow_id || 'Workflow unknown';
    const started = parseTimestamp(run.started_at || '');
    const open = el('button', {type: 'button', class: 'workflow-open',
      'aria-label': `${workflow}, started ${started ? relativePhrase(started) : 'at an unknown time'}`}, workflow);
    open.addEventListener('click', () => openRun(run));
    const models = [...new Set((run.calls || []).map(call => call.model).filter(Boolean))];
    const tokensIn = (run.calls || []).reduce((sum, call) => sum + Number(call.tokens_in || 0), 0);
    const tokensOut = (run.calls || []).reduce((sum, call) => sum + Number(call.tokens_out || 0), 0);
    const url = n8nUrl(run.instance_id, run.workflow_id, run.execution_id);
    return [
      {sortValue: workflow.toLowerCase(), content: open},
      instanceName(run.instance_id),
      {sortValue: started ? started.getTime() : 0, content: relativeTime(run.started_at)},
      models.join(', ') || 'Not reported',
      {sortValue: tokensIn + tokensOut, content: `${count(tokensIn)} in · ${count(tokensOut)} out`},
      {sortValue: Number(run.known_cost_usd || 0), content: costText(run)},
      {sortValue: '', content: rowMenu([
        {label: 'Show AI calls', glyph: 'observe', onSelect: () => openRun(run)},
        url ? {label: 'Open execution in n8n', href: url, external: true} : null,
      ], `More actions for ${workflow}, execution ${run.execution_id}`)},
    ];
  }

  async function loadRuns(before = null) {
    const params = new URLSearchParams({limit: String(pageSize()), ai: 'true'});
    if (instanceId) params.set('instance_id', instanceId);
    if (before) params.set('before', before);
    olderRuns.disabled = true;
    try {
      const result = await api('/api/otel/runs?' + params, {signal});
      if (signal.aborted) return;
      listCursor = result.next_cursor;
      if (before && runsTable) runsTable.appendRows(result.data.map(runRow));
      else if (result.data.length) {
        runsTable = table(['Workflow', 'Instance', 'Started', 'Model', 'Tokens', 'AI cost', ''], result.data.map(runRow), {
          numeric: [4, 5], sortable: {0: 'text', 1: 'text', 2: 'number', 4: 'number', 5: 'number'}, initialSort: {column: 2, direction: 'desc'}});
        runs.replaceChildren(el('h2', {class: 'result-card-title'}, 'Runs with AI calls'), runsTable);
      }
      return result;
    } finally {
      olderRuns.disabled = false;
      olderRuns.hidden = !listCursor;
    }
  }

  // One check per instance, in turn: n8n answers one request at a time per instance anyway.
  async function checkInstances(older) {
    const results = [];
    for (const instance of scope) {
      if (older && !scanCursors.get(instance)) continue;
      try {
        const result = await api('/api/otel/runs/scan', {method: 'POST', signal,
          json: {instance_id: instance, ...(older ? {cursor: scanCursors.get(instance)} : {})}});
        scanCursors.set(instance, result.next_cursor || null);
        results.push({instance, result});
      } catch (error) {
        if (signal.aborted || error.name === 'AbortError') throw error;
        results.push({instance, error});
      }
    }
    return results;
  }

  async function check(older = false) {
    if (busy || signal.aborted) return;
    busy = true;
    action.disabled = checkOlder.disabled = true;
    messages.replaceChildren(el('p', {class: 'load-progress', role: 'status'},
      older ? 'Reading older runs from n8n…' : `Reading new runs from n8n for ${scopeName}…`));
    try {
      const results = scope.length ? await checkInstances(older) : [];
      const params = new URLSearchParams({days: String(DAYS)});
      if (instanceId) params.set('instance_id', instanceId);
      const [summary, listed] = await Promise.all([api('/api/otel/runs/summary?' + params, {signal}), loadRuns()]);
      if (signal.aborted) return;
      messages.replaceChildren();
      paintStrip(summary);
      paintNotes(results, summary);
      paintWorkflows(summary);
      runs.closest('section').hidden = false;
      if (!scope.length) runs.replaceChildren(empty('No n8n connection yet', 'Connect an n8n instance to see what its AI steps cost.',
        [el('a', {class: 'button ghost', href: '#instances'}, 'Add connection')], 'observe'));
      // The list covers the whole retention period, the totals the last seven days.
      else if (!listed?.data.length) {
        runsTable = null;
        runs.replaceChildren(summary.runs
          ? empty('No AI calls in the runs checked', `${plural(summary.runs, 'run')} from the last ${DAYS} days checked. None called an AI model through an AI node or an HTTP Request node.`, [], 'observe')
          : empty('No finished runs yet', `Runs from the last ${DAYS} days appear here once n8n has finished them.`, [], 'observe'));
      }
    } catch (error) {
      if (!signal.aborted && error.name !== 'AbortError') {
        paintStrip(null);
        messages.replaceChildren(loadFailure(error, () => check(older), `Could not load AI cost for ${scopeName}`));
      }
    } finally {
      busy = false;
      action.disabled = checkOlder.disabled = false;
    }
  }

  messages.replaceChildren(loadingState(`Loading AI cost for ${scopeName}…`));
  return {node, action, load: () => check(false)};
}
