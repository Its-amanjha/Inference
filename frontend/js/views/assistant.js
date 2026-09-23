import {providerSetup,providerLabel} from '../lib/provider-setup.js';
import {api} from '../api.js';
import {can, state} from '../state.js';
import {el, field, formCard, heading, notice} from '../lib/html.js';
import {icon,actionIcon} from '../lib/icons.js';
import {rawDisclosure, instanceName, toneBadge, formatTime} from '../lib/payload.js';
import {confirmAction} from '../lib/confirm.js';
import {selectMenu} from '../lib/select-menu.js';
import {relativeTime} from '../lib/triage.js';
import {parseRepairTarget, repairHash, repairContext, contextQuery, describeTarget, listWorkflows, targetChip, createIdPicker, MAX_NOTES_CHARS} from '../lib/repair-target.js';
import {isRepairAction, isRound2Action, proposalShell, renderProposal, effectiveStatus, statusBadge, statusForError, describeRepairError, preconditionsOf, preconditionKey,
  turnSteps, contextSent, contextOverview, RATE_LIMITED_TEXT, INCOMPLETE_TEXT, RETRY_WARNING} from '../lib/proposals.js';

// Real approval statuses from the assistant API; unknown values are shown verbatim.
const APPROVAL_STATUS = {pending: ['Pending', 'info', 'clock'], executing: ['Executing', 'info', 'refresh'], succeeded: ['Succeeded', 'success', 'check'],
  failed: ['Failed', 'error', 'blocked'], stale: ['Stale', 'warning', 'errors'], cancelled: ['Cancelled', 'neutral', 'close'],
  expired: ['Expired', 'neutral', 'clock'], uncertain: ['Uncertain', 'warning', 'errors']};
const APPROVAL_TEXT = {pending: 'Nothing runs until you confirm.', executing: 'Running now. Wait for the outcome before requesting another action.',
  succeeded: 'Completed.', failed: 'The action failed.', stale: 'Not run: the target changed after this proposal. Create a new proposal.',
  cancelled: 'Cancelled. Nothing ran.', expired: 'Expired before it ran.'};
const TOOL_SUPPORT = {supported: ['Supports repair: yes', 'success'], unsupported: ['Supports repair: no', 'warning'], untested: ['Supports repair: untested', '']};
const UNTESTED_TEXT = "This model hasn't passed the tool-calling check, so it may explain the error without proposing a change.";
const UNSUPPORTED_TEXT = "This model can't propose changes; choose another model.";
const ERROR_TEXT_OFF = 'An administrator turned off error text in repair conversations.';
const DIRECTORY_OFF = 'An administrator turned off sharing other workflows’ trigger addresses with the AI.';

export async function render(container) {
  const controller = new AbortController();
  const {signal} = controller;
  container.classList.add('assistant-page');
  const request = (path, options = {}) => api(`/api/assistant${path}`, {...options, signal});
  const listen = (node, event, handler) => node.addEventListener(event, handler, {signal});
  const action = (label, handler, primary = false) => {
    const node = el('button', {type: 'button', className: primary ? 'button button-primary' : 'button button-ghost'}, actionIcon(label),label);
    listen(node, 'click', async () => {
      node.disabled = true;
      try { await handler(); } catch (error) { if (!signal.aborted) notice(messages, error); }
      finally { if (!signal.aborted) node.disabled = false; }
    });
    return node;
  };
  // Announce complete events, not transcript mutations or shared-renderer redraws.
  const polite = el('div', {className: 'sr-only assistant-announcement', role: 'status', 'aria-atomic': 'true'});
  const urgent = el('div', {className: 'sr-only assistant-announcement', role: 'alert', 'aria-atomic': 'true'});
  const announce = (text, interrupt = false) => {
    if (signal.aborted) return;
    (interrupt ? polite : urgent).textContent = '';
    (interrupt ? urgent : polite).textContent = text;
  };
  const messages = el('div', {className:'assistant-notices'});
  // The model is chosen in the composer next to the mode, as in code assistants. A different model
  // starts a new conversation.
  const provider = selectMenu({label: 'Model', options: [{value: '', label: 'Loading models…'}], className: 'composer-select',
    onChange: () => { chatId = null; resumeId.value = ''; showWelcome(); paintToolSupport(); }});
  provider.disabled = true;
  const transcript = el('div', {className: 'chat-transcript', role: 'region', 'aria-label': 'Conversation'});
  const input = el('textarea', {rows: 1, maxLength: 16000, required: true, disabled:true, placeholder: 'Ask about an execution or workflow…'});
  const approvals = el('div', {className: 'stack'});
  let chatId = null;
  let busy = false;
  let approvalRows = [];
  let providerRows = [];
  let modelsAvailable = false;
  let settings = null;
  let lastMessage = '';
  // Settings live in a side drawer (built below for administrators), not on the work page.
  let openSettings = () => {};
  const acting = new Set();
  const proposals = new Map();
  const loadingActions = new Set();
  const workflowNames = new Map();
  const announcedStates = new Map(), proposalOperations = new Map();
  const announceProposal = (row, status) => {
    const previous = announcedStates.get(row.id);
    announcedStates.set(row.id, status);
    // Loading history is silent, and a rail/card repaint is not a new state change.
    if (previous === undefined || previous === status) return;
    const target = row.target || row.arguments || {};
    const subject = target.workflow_name || target.workflow_id || row.name || 'action';
    const where = target.instance_id ? ` on ${target.instance_name || instanceName(target.instance_id)}` : '';
    const execution = target.execution_id ? `, execution ${target.execution_id}` : '';
    const label = {executing: proposalOperations.get(row.id) === 'cancel' ? 'Cancelling' : 'Applying', succeeded: 'Applied',
      failed: 'Failed', uncertain: 'Outcome unknown', stale: 'Stale', cancelled: 'Cancelled', expired: 'Expired', pending: 'Ready for review'}[status] || status;
    const next = status === 'uncertain' ? ' It may have run. Check n8n before taking another action.'
      : status === 'failed' ? ' Review the error before requesting another proposal.' : '';
    announce(`${label}: proposal for ${subject}${where}${execution}.${next}`, status === 'uncertain');
  };
  // An empty conversation offers the open failures it can start from, newest first. Picking one fills the
  // message; nothing is sent until the person sends it.
  const showWelcome = () => {
    if(!modelsAvailable){
      const welcome=el('div',{className:'assistant-welcome'},el('h2',{},'Connect a model to get started'),
        el('p',{},can('admin')?'Choose a local or hosted model in Assistant settings.':'Ask your administrator to connect a model.'));
      if(can('admin'))welcome.append(action('Connect model',()=>openSettings(),true));
      transcript.replaceChildren(welcome);return;
    }
    const starts=el('ul',{className:'assistant-starts'});
    const welcome=el('div',{className:'assistant-welcome'},el('h2',{},'Start from a recent failure'),starts,
      el('p',{className:'assistant-welcome-note'},'Or ask about any workflow or execution. Nothing changes unless you switch to Propose changes and approve what the AI proposes.'));
    transcript.replaceChildren(welcome);
    const noStarts=()=>{welcome.querySelector('h2').textContent='Ask about a workflow or execution';starts.remove();
      welcome.querySelector('.assistant-welcome-note').textContent='Nothing changes unless you switch to Propose changes and approve what the AI proposes.';};
    const query=new URLSearchParams({status:'open',limit:'4'});if(state.instanceId)query.set('instance_id',state.instanceId);
    api('/api/errors?'+query,{signal}).then(result=>{
      const rows=Array.isArray(result?.data)?result.data:[];
      if(!rows.length){noStarts();return;}
      starts.replaceChildren(...rows.map(row=>{
        const where=`${row.node_name||'Workflow'} · ${instanceName(row.instance_id)}`;
        const pick=el('button',{type:'button',className:'assistant-start'},icon('errors'),
          el('span',{className:'assistant-start-text'},el('strong',{},`Diagnose ${row.workflow_name||'a workflow'}`),el('span',{},where,' · ',relativeTime(row.occurred_at))));
        pick.addEventListener('click',()=>{
          input.value=`Help me investigate error ${row.id} in workflow “${row.workflow_name||row.workflow_id}” (${row.node_name||'workflow'}) on ${instanceName(row.instance_id)}. Use available metadata and suggest next steps.`;
          setMode('ask');scheduleComposerSize();input.focus();
        });
        return el('li',{},pick);
      }));
    },()=>noStarts());
  };

  // Repair target, "What the AI sees" and model support. Only people pin or change the target.
  const parsed = parseRepairTarget();
  let target = parsed.target;
  let errorTextAllowed = true;
  let allowErrorText = null;
  let repairFeedback = null;
  const sendErrorText = el('input', {type: 'checkbox', checked: true});
  const errorTextNote = el('p', {className: 'repair-admin-note', hidden: true}, ERROR_TEXT_OFF);
  // Native maxlength counts UTF-16 units; the input handler enforces Python's code-point limit.
  const operatorNotes = el('textarea', {rows: 2, maxLength: MAX_NOTES_CHARS * 2, 'aria-describedby': 'repair-notes-hint repair-notes-remaining', placeholder: 'What should the AI know about this repair?'});
  const notesRemaining = el('span', {id: 'repair-notes-remaining'}, `${MAX_NOTES_CHARS} characters remaining`);
  const notesField = el('div', {className: 'repair-notes'}, field('Operator note (optional)', operatorNotes),
    el('p', {id: 'repair-notes-hint', className: 'muted'}, 'Sent to the AI with your next message. Review the server-masked text in What the AI sees.'), notesRemaining);
  const contextBody = el('div', {className: 'repair-context-body'});
  const contextDetails = el('details', {className: 'repair-context'}, el('summary', {}, 'What the AI sees'),
    el('div', {className: 'repair-context-panel'}, el('label', {className: 'repair-check'}, sendErrorText, el('span', {}, 'Send scrubbed error text to the AI')), errorTextNote, contextBody));
  const modelNotice = el('div', {className: 'repair-model-notice', role: 'status', hidden: true});
  const chip = targetChip({signal, onChange: next => setTarget(next, true)});
  const repairBar = el('div', {className: 'repair-bar', hidden: true}, chip.node, notesField, contextDetails, modelNotice);
  const errorTextEnabled = () => errorTextAllowed && sendErrorText.checked;
  const applyErrorTextSetting = enabled => {
    const allowed = enabled !== false;
    if (allowed !== errorTextAllowed) sendErrorText.checked = allowed;
    errorTextAllowed = allowed; sendErrorText.disabled = !allowed; errorTextNote.hidden = allowed;
    if (allowErrorText) allowErrorText.checked = allowed;
  };
  // One masked context read per target and error-text choice. It names the workflow in the chip and fills What the AI sees,
  // so the repair UI never needs the raw workflow document.
  let contextSequence = 0, contextKey = '', contextPromise = null, contextResult = null;
  const resetContext = (collapse = false) => {
    contextKey = ''; contextPromise = null; contextResult = null; contextSequence++;
    contextBody.replaceChildren();
    if (collapse) contextDetails.open = false;
  };
  const fetchContext = () => {
    if (!target) return Promise.resolve(null);
    const query = contextQuery(target, errorTextEnabled(), operatorNotes.value).toString();
    if (query !== contextKey || !contextPromise) {
      contextKey = query; contextResult = null;
      const pending = request(`/repair/context?${query}`);
      contextPromise = pending;
      pending.then(result => { if (contextPromise === pending) contextResult = result; }, () => { if (contextPromise === pending) { contextKey = ''; contextPromise = null; } });
    }
    return contextPromise;
  };
  const loadContext = async (retried = false) => {
    if (!target || !contextDetails.open) return;
    const current = ++contextSequence;
    if (!contextResult) contextBody.replaceChildren(el('p', {className: 'muted', role: 'status'}, 'Loading what the AI sees…'));
    try {
      const result = await fetchContext();
      if (!signal.aborted && current === contextSequence && result) contextBody.replaceChildren(...contextOverview(result));
    } catch (error) {
      if (signal.aborted || current !== contextSequence) return;
      if (error.code === 'repair_error_text_disabled' && !retried) { applyErrorTextSetting(false); await loadContext(true); return; }
      contextBody.replaceChildren(); notice(contextBody, error);
    }
  };
  const resolveTargetName = (retried = false) => {
    const pinned = target;
    fetchContext().then(result => {
      const name = result?.workflow?.name;
      if (!signal.aborted && pinned === target && typeof name === 'string' && name.trim()) chip.setName(name);
    }, error => {
      if (!signal.aborted && pinned === target && error.code === 'repair_error_text_disabled' && !retried) { applyErrorTextSetting(false); resolveTargetName(true); }
    });
  };
  listen(contextDetails, 'toggle', () => { if (contextDetails.open) loadContext(); });
  listen(sendErrorText, 'change', () => loadContext());
  let notesTimer;
  listen(operatorNotes, 'input', () => {
    const characters = Array.from(operatorNotes.value);
    if (characters.length > MAX_NOTES_CHARS) operatorNotes.value = characters.slice(0, MAX_NOTES_CHARS).join('');
    notesRemaining.textContent = `${Math.max(0, MAX_NOTES_CHARS - characters.length)} characters remaining`;
    clearTimeout(notesTimer);
    resetContext();
    if (contextDetails.open) {
      contextBody.append(el('p', {className: 'muted', role: 'status'}, 'Updating the masked note…'));
      notesTimer = setTimeout(() => loadContext(), 350);
    }
  });

  const selectedProvider = () => providerRows.find(row => row.id === provider.value);
  const MODES = [{value: 'ask', label: 'Ask', glyph: 'eye', detail: 'Reads and answers. Cannot propose changes.'},
    {value: 'propose', label: 'Propose changes', glyph: 'edit', detail: 'May propose changes. You approve each one.'},
    ...(can('operator') ? [{value: 'create', label: 'Create workflow', glyph: 'add', detail: 'Plans a new inactive workflow for your review.'}] : [])];
  let mode = 'ask';
  const composerWarning = el('p', {className: 'composer-warning', role: 'status', hidden: true});
  const toolStatus = row => Object.hasOwn(TOOL_SUPPORT, row?.tool_calling?.status ?? '') ? row.tool_calling.status : 'untested';
  const toolBadge = row => {
    const status = toolStatus(row), [label, tone] = TOOL_SUPPORT[status];
    return el('span', {className: `badge tool-calling-badge${tone ? ` ${tone}` : ''}`, 'data-status': status,
      title: row?.tool_calling?.checked_at ? `Checked ${formatTime(row.tool_calling.checked_at)}` : 'Not checked yet'}, label);
  };
  let paintModels = () => {};
  const paintToolSupport = () => {
    const row = selectedProvider();
    const status = toolStatus(row);
    // In the composer, only a limit is worth a line: an untested or unsupported model in a change mode.
    const limits = Boolean(row && status !== 'supported' && mode !== 'ask');
    composerWarning.hidden = !limits;
    composerWarning.replaceChildren(...(limits ? [icon('info'), el('span', {}, status === 'unsupported' ? UNSUPPORTED_TEXT : UNTESTED_TEXT)] : []));
    // Diagnosis stays available either way; the notice only sets expectations about proposals.
    const show = Boolean(target && row && status !== 'supported');
    modelNotice.hidden = !show;
    modelNotice.replaceChildren(...(show ? [icon('info'), el('p', {}, status === 'unsupported' ? UNSUPPORTED_TEXT : UNTESTED_TEXT)] : []));
  };
  const checkToolCalling = async (row, feedback = messages) => {
    if (!row) return;
    feedback.replaceChildren();
    let result;
    try { result = await request(`/providers/${encodeURIComponent(row.id)}/tool-test`, {method: 'POST'}); }
    catch (error) { if (error.code !== 'provider_rate_limited') throw error; if (!signal.aborted) notice(feedback, RATE_LIMITED_TEXT); return; }
    if (signal.aborted) return;
    // The result may be nested under tool_calling or sent at the top level; a failed check never changes the stored result.
    const checked = result?.tool_calling && typeof result.tool_calling === 'object' ? result.tool_calling : result;
    const status = typeof checked?.status === 'string' ? checked.status : 'untested';
    row.tool_calling = {status, checked_at: checked?.checked_at ?? null};
    paintToolSupport(); paintModels(row);
    const summary = {supported: 'Tool calling works with this model, so it can propose repairs.', unsupported: 'This model did not pass the tool-calling check, so it cannot propose repairs.',
      untested: 'The tool-calling check did not finish; this model is still untested.'}[status] || `The tool-calling check returned ${status}.`;
    notice(feedback, result?.detail ? `${summary} ${result.detail}` : summary);
  };

  const paintTarget = () => { chip.set(target); repairBar.hidden = !target; resetContext(); if (target) { resolveTargetName(); loadContext(); } paintToolSupport(); };
  const setTarget = (next, announce = false) => {
    const hadFocus = repairBar.contains(document.activeElement);
    target = next;
    paintTarget();
    // Keep the address shareable without re-rendering the workspace.
    history.replaceState(history.state, '', `${location.pathname}${location.search}${target ? repairHash(target) : '#assistant'}`);
    if (announce && transcript.querySelector('.chat-message')) {
      const pinned = target;
      const note = el('p', {className: 'chat-event'}, pinned ? `Repair target changed to ${describeTarget(pinned)}.` : 'Repair target cleared. New messages no longer include repair context.');
      transcript.append(note);
      if (pinned) fetchContext().then(result => { const name = result?.workflow?.name; if (name && !signal.aborted && pinned === target) note.textContent = `Repair target changed to ${describeTarget(pinned, name)}.`; }, () => {});
    }
    if (!target && hadFocus && !input.disabled) input.focus();
  };

  const refreshProviders = async () => {
    const selected = provider.value;
    const result = await request('/config/provider');
    if (signal.aborted) return;
    providerRows = Array.isArray(result.data) ? result.data : [];
    modelsAvailable=Boolean(providerRows.length);
    provider.setOptions(modelsAvailable ? providerRows.map(item => ({value: item.id, label: item.model, detail: `Connection ${item.id}`}))
      : [{value: '', label: 'No model connected'}], selected);
    send.disabled = !modelsAvailable||busy;
    input.disabled = !modelsAvailable;
    provider.disabled = !modelsAvailable||busy;
    paintToolSupport(); paintModels();
    if(!transcript.querySelector('.chat-message'))showWelcome();
  };
  // Names are for readability only; the approval stays bound to the IDs shown. They come from the approval's own
  // target or masked workflow metadata (id, name, active), never from raw workflow documents.
  const workflowLabel = (instanceId, workflowId, knownName = null) => {
    const named = name => `workflow “${name}” (${workflowId})`;
    if (typeof knownName === 'string' && knownName.trim()) return el('span', {}, named(knownName));
    const label = el('span', {}, `workflow ${workflowId}`);
    if (typeof instanceId !== 'string' || !instanceId || typeof workflowId !== 'string' || !workflowId) return label;
    if (!workflowNames.has(instanceId) && workflowNames.size < 20) workflowNames.set(instanceId, listWorkflows(instanceId, signal).then(rows => new Map(rows.map(row => [row.id, row.name])), () => new Map()));
    workflowNames.get(instanceId)?.then(names => { const name = names.get(workflowId); if (name && name !== workflowId && !signal.aborted) label.textContent = named(name); });
    return label;
  };
  const summarize = row => {
    const args = row.arguments && typeof row.arguments === 'object' ? row.arguments : {};
    const on = ` on ${instanceName(args.instance_id)}`;
    const known = row.target && typeof row.target === 'object' && String(row.target.workflow_id) === String(args.workflow_id) ? row.target.workflow_name : null;
    const workflowLabelFor = () => workflowLabel(args.instance_id, args.workflow_id, known);
    if (row.name === 'activate_workflow') return ['Activate ', workflowLabelFor(), on];
    if (row.name === 'deactivate_workflow') return ['Deactivate ', workflowLabelFor(), on];
    if (row.name === 'delete_execution') return [`Permanently delete execution ${args.execution_id}${on}`];
    if (row.name === 'update_node') return [`Replace the code of node ${args.node_id} in `, workflowLabelFor(), on];
    if (row.name === 'mcp_call') return [`Call remote tool “${args.tool_name}” on MCP server ${args.server_id}`];
    if (row.name === 'propose_workflow_patch') return ['Change ', workflowLabelFor(), on];
    if (row.name === 'propose_restore_workflow') return ['Restore ', workflowLabelFor(), on];
    return [`Run ${row.name}`, args.instance_id ? on : ''];
  };

  // MCP tool results are arbitrary remote content. Every string below travels as a
  // text node, never as HTML, and URLs stay plain text: tool output is data, never
  // instructions. One sentence says which tool ran where and what happened; the full
  // payload stays behind a disclosure, and long output is capped inline.
  const MCP_INLINE_LIMIT = 1200;
  const mcpBlockText = block => (block && typeof block === 'object' && block.type === 'text' && typeof block.text === 'string' ? block.text : null);
  const mcpTextValue = text => {
    // JSON objects often arrive in text blocks, even when structuredContent is absent.
    if (text.length <= 100000 && /^[\s]*[\[{]/.test(text)) {
      try { return JSON.parse(text); } catch { /* Ordinary text remains literal. */ }
    }
    return text;
  };
  const renderMcpResult = (row, result) => {
    const args = row.arguments && typeof row.arguments === 'object' ? row.arguments : {};
    const head = `Tool “${args.tool_name || 'unknown tool'}” on MCP server ${args.server_id || 'unknown server'}`;
    const section = el('section', {className: 'mcp-result', 'aria-label': 'MCP tool result'});
    let remaining = MCP_INLINE_LIMIT, entries = 60, shortened = false;
    const valueNode = (value, depth = 0) => {
      if (entries-- <= 0 || remaining <= 0) { shortened = true; return null; }
      if (value !== null && typeof value === 'object') {
        if (depth >= 4) { shortened = true; return el('p', {className: 'muted'}, 'Nested content is available in Raw MCP result.'); }
        const array = Array.isArray(value), keys = Object.keys(value);
        if (!keys.length) return el('p', {className: 'muted'}, array ? 'No items.' : 'No fields.');
        const list = el(array ? 'ol' : 'dl', {className: array ? 'mcp-result-list' : 'mcp-result-fields'});
        for (const key of keys) {
          const child = valueNode(value[key], depth + 1);
          if (!child) break;
          if (array) list.append(el('li', {}, child));
          else {
            const label = key.replace(/([a-z0-9])([A-Z])/g, '$1 $2').replace(/[_-]+/g, ' ').replace(/^./, c => c.toUpperCase());
            if (label.length > 120) shortened = true;
            list.append(el('dt', {}, label.length > 120 ? `${label.slice(0, 120)}…` : label), el('dd', {}, child));
          }
        }
        return list;
      }
      const text = value === null ? 'Not provided.' : value === true ? 'Yes' : value === false ? 'No' : value === '' ? 'Empty text.' : String(value);
      const shown = text.slice(0, remaining);
      remaining -= shown.length;
      if (shown.length < text.length) shortened = true;
      return el('p', {className: 'mcp-result-text'}, shown + (shown.length < text.length ? '…' : ''));
    };
    const done = (sentence, values = []) => {
      section.append(el('p', {className: 'mcp-result-head'}, sentence));
      const preview = el('div', {className: 'mcp-result-preview', tabIndex: 0, 'aria-label': 'Tool output'});
      for (const value of values) { const node = valueNode(value); if (!node) break; preview.append(node); }
      if (preview.childNodes.length) section.append(preview);
      if (shortened) section.append(el('p', {className: 'muted'}, 'Preview shortened. Open Raw MCP result for the complete output.'));
      section.append(rawDisclosure(result, 'Raw MCP result'));
      return section;
    };
    if (result === null || result === undefined) return done(`${head} returned no content.`);
    if (typeof result !== 'object' || Array.isArray(result)) return done(`${head} returned an unrecognized result shape.`, [result]);
    const blocks = Array.isArray(result.content) ? result.content : [];
    const structured = result.structuredContent !== undefined;
    const values = structured ? [result.structuredContent] : [];
    const kinds = new Map();
    const structuredText = structured ? JSON.stringify(result.structuredContent) : null;
    for (const block of blocks) {
      const text = mcpBlockText(block);
      if (text !== null) {
        if (text.trim() && text.trim() !== structuredText) values.push(mcpTextValue(text));
      } else if (block?.type === 'resource_link' || (block?.type === 'resource' && block.resource && typeof block.resource === 'object')) {
        const resource = block.type === 'resource' ? block.resource : block;
        const detail = {resource: block.type === 'resource' ? 'Embedded resource' : 'Linked resource'};
        for (const key of ['name', 'uri', 'mimeType', 'description']) {
          if (typeof resource[key] === 'string') detail[key] = resource[key];
        }
        if (typeof resource.text === 'string') detail.content = mcpTextValue(resource.text);
        else if (typeof resource.blob === 'string') detail.content = 'Binary content is available in Raw MCP result.';
        // Resource URLs are identifiers, not permission to fetch or navigate.
        values.push(detail);
      } else {
        const kind = block && typeof block.type === 'string' ? block.type : 'unknown';
        kinds.set(kind, (kinds.get(kind) || 0) + 1);
      }
    }
    if (result.isError === true) {
      if (typeof result.message === 'string' && result.message) values.unshift(mcpTextValue(result.message));
      return done(`${head} reported an error.`, values.length ? values : ['The tool reported an error without a message.']);
    }
    const attachments = [...kinds].map(([kind, count]) => `${count} ${kind} attachment${count === 1 ? '' : 's'} (listed, not shown)`);
    const count = values.length + [...kinds.values()].reduce((sum, count) => sum + count, 0);
    if (!count) return done(`${head} returned ${Array.isArray(result.content) ? 'no content' : 'an unrecognized result shape'}.`);
    return done(`${head} returned ${count} item${count === 1 ? '' : 's'}.${attachments.length ? ` Also returned: ${attachments.join(', ')}.` : ''}`, values);
  };

  // Inline repair proposals: one card per action, painted in place wherever it sits in the conversation.
  const fetchAction = async id => {
    try {
      const result = await request(`/approvals/${encodeURIComponent(id)}`);
      const item = result?.pending_action && typeof result.pending_action === 'object' ? result.pending_action : result;
      if(isRound2Action(item)&&['succeeded','uncertain'].includes(item.status)){
        try{item.savedResult=(await request(`/repair/${encodeURIComponent(id)}/reconcile`))?.result??null;}catch{/* Keep the confirmed status even if saved details are unavailable. */}
      }
      return item && typeof item === 'object' && item.id ? item : null;
    } catch { return null; }
  };
  const confirmTool = id => request('/tools/confirm', {method: 'POST', json: {action_id: id}});
  const targetOf = model => {
    const value = model.action.target && typeof model.action.target === 'object' ? model.action.target : model.action.arguments;
    return value && typeof value === 'object' ? value : {};
  };
  const targetNodes = value => [el('strong', {}, value.workflow_name || value.workflow_id || 'this workflow'), ' on ', el('strong', {}, value.instance_name || instanceName(value.instance_id))];
  const paintProposal = model => {
    const previous = model.painted;
    model.painted = renderProposal(model.card, {...model, canOperate: can('operator'), acting: acting.has(model.action.id)}, handlersFor(model));
    // Keep the shared card's visible text, but let this view announce each change once.
    model.card.querySelectorAll('[role="status"],[role="alert"],[aria-live]').forEach(node => {
      node.removeAttribute('role'); node.removeAttribute('aria-live');
    });
    announceProposal(model.action, model.painted);
    if (previous === model.painted && model.feedback && model.feedback !== model.announcedFeedback) {
      announce(model.feedback.error ? describeRepairError(model.feedback.error) : model.feedback.text);
    }
    model.announcedFeedback = model.feedback;
  };
  const mountProposal = (item, anchor = null) => {
    let model = proposals.get(item.id);
    if (model) { model.action = {...model.action, ...item}; if (item.status && !acting.has(item.id)) model.status = item.status; }
    else { model = {action: item, status: item.status || 'pending', result: null, reconcile: null, feedback: null, deactivated: new Set(), forceActive: false, activated: false, card: proposalShell(item.id)}; proposals.set(item.id, model); }
    if (anchor) anchor.after(model.card); else if (!model.card.isConnected) transcript.append(model.card);
    paintProposal(model);
    if(item.savedResult){model.result=item.savedResult;paintProposal(model);}
    return model;
  };
  const applyProposal = async model => {
    const id = model.action.id, apply = model.action.preview?.apply || {};
    const executionRetry = model.action.preview?.kind === 'execution_retry';
    if (acting.has(id)) return;
    if (executionRetry || apply.danger || apply.typed_confirmation) {
      const ok = await confirmAction({title: executionRetry ? 'Review execution retry' : apply.label || 'Apply change', target: executionRetry ? [`Execution ${targetOf(model).execution_id} of `, ...targetNodes(targetOf(model))] : targetNodes(targetOf(model)), consequence: executionRetry ? RETRY_WARNING : model.action.preview?.summary_text || 'This writes the change to the workflow.',
        confirmLabel: executionRetry ? 'Retry execution' : apply.label || 'Apply change', danger: executionRetry || Boolean(apply.danger), requireText: executionRetry ? 'RETRY' : apply.typed_confirmation || undefined});
      if (!ok || signal.aborted) return;
    }
    proposalOperations.set(id, 'apply'); acting.add(id); model.feedback = null; paintProposal(model);
    try {
      const outcome = await confirmTool(id);
      if (signal.aborted) return;
      model.status = outcome?.status || (isRound2Action(model.action) ? 'uncertain' : 'succeeded'); model.result = outcome?.result ?? null; model.forceActive = false;
    } catch (error) {
      if (signal.aborted) return;
      model.status = statusForError(error.code) || (isRound2Action(model.action) ? (error.status ? 'failed' : 'uncertain') : model.status);
      if (error.code === 'workflow_active') { model.forceActive = true; model.deactivated.clear(); }
      model.feedback = {error};
    } finally {
      acting.delete(id);
      if (!signal.aborted) { await refreshApprovals().catch(() => {}); paintProposal(model); }
    }
  };
  const cancelProposal = async model => {
    const id = model.action.id;
    if (acting.has(id)) return;
    proposalOperations.set(id, 'cancel'); acting.add(id); model.feedback = null; paintProposal(model);
    try { const outcome = await request('/tools/cancel', {method: 'POST', json: {action_id: id}}); if (!signal.aborted) model.status = outcome?.status || 'cancelled'; }
    finally { acting.delete(id); if (!signal.aborted) { await refreshApprovals().catch(() => {}); paintProposal(model); } }
  };
  const deactivateFirst = async (model, item) => {
    const value = targetOf(model);
    const ok = await confirmAction({title: 'Deactivate workflow', target: targetNodes(value), consequence: 'Its triggers stop, so it no longer runs automatically on this instance until it is activated again. The proposed change is not applied yet.', confirmLabel: 'Deactivate workflow'});
    if (!ok || signal.aborted) return;
    model.feedback = null;
    let outcome = null;
    if (item.action_id) {
      // A linked proposal that was already used or went stale gets a fresh one for the same workflow.
      try { outcome = await confirmTool(item.action_id); } catch (error) { if (error.status !== 409) throw error; }
    }
    if (!outcome) {
      const created = await request('/tools/run', {method: 'POST', json: {name: 'deactivate_workflow', arguments: {instance_id: value.instance_id, workflow_id: value.workflow_id}}});
      if (!created?.pending_action?.id) throw new Error('The server did not return a deactivation proposal.');
      outcome = await confirmTool(created.pending_action.id);
    }
    if (signal.aborted) return;
    if (outcome?.status === 'succeeded') {
      for (const entry of [item, ...preconditionsOf(model)]) if (entry.code === 'workflow_active') model.deactivated.add(preconditionKey(entry));
      model.forceActive = false; model.feedback = {text: 'Workflow deactivated. You can apply the change now.'};
    } else model.feedback = {text: `Deactivation status: ${outcome?.status || 'unknown'}. Check the workflow before applying.`};
    const fresh = await fetchAction(model.action.id);
    if (signal.aborted) return;
    if (fresh?.preview) model.action = {...model.action, ...fresh};
    await refreshApprovals().catch(() => {});
    paintProposal(model);
  };
  const askReread = async model => {
    if (busy) throw new Error('Wait for the current reply before asking again.');
    if (!modelsAvailable || !provider.value) throw new Error('Connect a model before asking the AI to re-read the workflow.');
    const value = targetOf(model);
    await sendTurn({message: `The proposal for workflow ${value.workflow_name ? `“${value.workflow_name}” (${value.workflow_id})` : value.workflow_id} is stale because the workflow changed. Read the current workflow again and propose the change again.`});
  };
  const reconcileProposal = async model => {
    const result = await request(`/repair/${encodeURIComponent(model.action.id)}/reconcile`);
    if (signal.aborted) return;
    model.reconcile = typeof result?.state === 'string' ? result.state : 'unknown'; model.feedback = null;
    if(isRound2Action(model.action)&&result?.result)model.result=result.result;
    paintProposal(model);
    announce(model.card.querySelector('.proposal-reconcile')?.textContent || 'Saved proposal details loaded. The action was not repeated.');
  };
  const undoRepair = async model => {
    const value = targetOf(model), result = model.result && typeof model.result === 'object' ? model.result : {};
    const instanceId = result.instance_id || value.instance_id, workflowId = result.workflow_id || value.workflow_id;
    let base = result.content_hash;
    if (!base) base = (await request(`/repair/context?${new URLSearchParams({instance_id: instanceId, workflow_id: workflowId, send_error_text: 'false'})}`))?.workflow?.content_hash;
    if (signal.aborted) return;
    if (!base) throw new Error('The current workflow version could not be read, so no restore was proposed.');
    const created = await request('/tools/run', {method: 'POST', json: {name: 'propose_restore_workflow', arguments: {instance_id: instanceId, workflow_id: workflowId, base_content_hash: base, repair_id: result.repair_id || model.action.id}, ...(chatId ? {chat_id: chatId} : {})}});
    if (signal.aborted) return;
    const pending = created?.pending_action;
    if (!pending?.id) throw new Error('The server did not return a restore proposal.');
    const full = pending.preview ? pending : (await fetchAction(pending.id)) || pending;
    if (signal.aborted) return;
    const restore = mountProposal(full, model.card);
    model.feedback = {text: 'A restore proposal is ready below. Nothing is restored until you apply it.'};
    paintProposal(model);
    await refreshApprovals().catch(() => {});
    restore.card.scrollIntoView({block: 'nearest'});
    restore.card.focus({preventScroll: true});
  };
  const activateWorkflow = async model => {
    const value = targetOf(model), result = model.result && typeof model.result === 'object' ? model.result : {};
    const ok = await confirmAction({title: 'Activate workflow', target: targetNodes(value), consequence: 'Its triggers start on this instance, so it can run automatically.', confirmLabel: 'Activate workflow'});
    if (!ok || signal.aborted) return;
    const created = await request('/tools/run', {method: 'POST', json: {name: 'activate_workflow', arguments: {instance_id: result.instance_id || value.instance_id, workflow_id: result.workflow_id || value.workflow_id}}});
    if (!created?.pending_action?.id) throw new Error('The server did not return an activation proposal.');
    const outcome = await confirmTool(created.pending_action.id);
    if (signal.aborted) return;
    model.activated = outcome?.status === 'succeeded';
    model.feedback = {text: model.activated ? 'Workflow activated.' : `Activation status: ${outcome?.status || 'unknown'}.`};
    await refreshApprovals().catch(() => {});
    paintProposal(model);
  };
  const handlersFor = model => ({apply: () => applyProposal(model), cancel: () => cancelProposal(model), deactivate: item => deactivateFirst(model, item),
    reread: () => askReread(model), reconcile: () => reconcileProposal(model), undo: () => undoRepair(model), activate: () => activateWorkflow(model),
    fail: error => { if (signal.aborted) return; model.feedback = {error}; paintProposal(model); }});

  const railItem = (row, status) => {
    const link = el('a', {className: 'repair-rail-link', href: location.hash || '#assistant'}, 'Show in conversation');
    if (['propose_create_workflow','propose_retry_execution'].includes(row.name)) link.setAttribute('aria-label', `Show proposal ${row.id} in conversation`);
    listen(link, 'click', event => { event.preventDefault(); reviewDrawer.close(); showInConversation(row).catch(error => { if (!signal.aborted) notice(messages, error); }); });
    const title = typeof row.preview_summary === 'string' && row.preview_summary ? row.preview_summary : summarize(row);
    return el('article', {className: 'repair-rail-item', 'data-action-id': row.id}, el('div', {className: 'repair-rail-head'}, el('h3', {}, title), statusBadge(status)),
      status === 'pending' ? el('p', {className: 'muted'}, `Expires ${formatTime(Number(row.expires_at))}`) : null, link);
  };
  const paintApprovals = () => {
    approvals.replaceChildren();
    // A resolved row created only to satisfy another proposal's precondition (linked_to) is already
    // reported inside that proposal, so a second rail card for it is a duplicate. A linked row that is
    // still pending stays: it can be confirmed on its own, and hiding it would strand it unseen.
    const rows = approvalRows.filter(row => !row.linked_to || row.status === 'pending');
    if (!rows.length) { approvals.append(el('div',{className:'approval-empty'},icon('check'),el('h3',{},'Nothing to review'),el('p',{},'When the AI proposes a change, it waits here for you.'))); return; }
    for (const row of rows) {
      const expired = Number(row.expires_at) * 1000 <= Date.now();
      const status = acting.has(row.id) ? 'executing' : row.status === 'pending' && expired ? 'expired' : row.status;
      announceProposal(proposals.get(row.id)?.action || row, status);
      if (isRepairAction(row)) { approvals.append(railItem(row, status)); continue; }
      const [label, tone, glyph] = APPROVAL_STATUS[status] || [String(status), 'neutral', 'unknown'];
      const card = el('article', {className: 'card approval-card'},
        el('div', {className: 'decision-head'}, el('h3', {}, ...summarize(row)), toneBadge(label, tone, glyph)),
        APPROVAL_TEXT[status] ? el('p', {}, APPROVAL_TEXT[status]) : null,
        status === 'pending' ? el('p', {className: 'muted'}, `Expires ${formatTime(Number(row.expires_at))}`) : null);
      if (row.name === 'update_node' && status === 'pending') card.append(el('p', {}, 'Review the new code under Exact arguments before confirming.'));
      if (row.name === 'mcp_call') card.append(el('p', {}, 'This remote tool can change data. Its remote resource state cannot be verified.'));
      if (status === 'uncertain') card.append(el('p', {}, 'Check the remote system before requesting another action. Its outcome is unknown.'));
      card.append(rawDisclosure(row.arguments, 'Exact arguments'));
      if (status === 'pending' && can('operator')) {
        const perform = async verb => {
          if (acting.has(row.id)) return;
          proposalOperations.set(row.id, verb);
          announceProposal(row, 'executing');
          acting.add(row.id);
          confirm.disabled = cancel.disabled = true;
          try {
            const result = await request(`/tools/${verb}`, {method: 'POST', json: {action_id: row.id}});
            if (!signal.aborted) {
              row.status = result.status;
              if (result.result !== undefined) {
                if (row.name === 'mcp_call') {
                  transcript.querySelector('.assistant-welcome')?.remove();
                  transcript.append(renderMcpResult(row, result.result));
                } else messages.append(el('div', {className: 'notice', role: 'status'}, `${verb === 'confirm' ? 'Done' : 'Cancelled'}: ${summarize(row).map(part => typeof part === 'string' ? part : part.textContent).join('')}.`),
                  rawDisclosure(result.result, 'Result details'));
              }
            }
          } finally { try { if (!signal.aborted) await refreshApprovals(); } finally { acting.delete(row.id); if (!signal.aborted) paintApprovals(); } }
        };
        const confirm = action('Confirm exact action', () => perform('confirm'), true);
        const cancel = action('Cancel', () => perform('cancel'));
        card.append(el('div', {className: 'actions'}, confirm, cancel));
      }
      approvals.append(card);
    }
  };
  // Proposals from this conversation that no message references appear newest first at the end.
  const mountConversationProposals = async () => {
    const rows = approvalRows.filter(row => isRepairAction(row) && chatId && row.chat_id === chatId && !proposals.get(row.id)?.card.isConnected && !loadingActions.has(row.id));
    rows.forEach(row => loadingActions.add(row.id));
    try {
      for (const row of rows) {
        const item = await fetchAction(row.id);
        if (signal.aborted) return;
        if (item && isRepairAction(item) && !proposals.get(row.id)?.card.isConnected) mountProposal({...item, status: row.status || item.status});
      }
    } finally { rows.forEach(row => loadingActions.delete(row.id)); }
  };
  const refreshApprovals = async () => {
    const result = await request('/approvals');
    if (signal.aborted) return;
    approvalRows = Array.isArray(result?.data) ? result.data : [];
    for (const row of approvalRows) {
      const model = isRepairAction(row) ? proposals.get(row.id) : null;
      if (model && row.status && !acting.has(row.id) && model.status !== row.status) { model.status = row.status; if (model.card.isConnected) paintProposal(model); }
    }
    paintApprovals();
    const pendingCount = approvalRows.filter(row => row.status === 'pending' && Number(row.expires_at) * 1000 > Date.now()).length;
    reviewCount.textContent = pendingCount ? String(pendingCount) : '';
    reviewButton.setAttribute('aria-label', pendingCount ? `Review, ${pendingCount} pending` : 'Review');
    reviewUpdated.textContent = `Updated ${clockTime()}`;
    mountConversationProposals();
  };

  // Chat turns. Each human action sends one request; nothing continues or retries on its own.
  const actionIds = meta => (Array.isArray(meta?.action_ids) ? meta.action_ids : Array.isArray(meta?.pending_actions) ? meta.pending_actions.map(item => item?.id) : [])
    .filter(id => typeof id === 'string' && id);
  const retryButton = turn => action('Try again', () => sendTurn({...turn, retry: !turn.continueTurn}));
  const turnFooter = (meta, turnMessage) => {
    const parts = [];
    const steps = Array.isArray(meta.steps) ? meta.steps : [];
    if (steps.some(step => step?.status === 'rate_limited' || step?.code === 'provider_rate_limited')) parts.push(el('p', {className: 'repair-turn-note'}, RATE_LIMITED_TEXT));
    if (meta.code === 'provider_incomplete' || steps.some(step => step?.code === 'provider_incomplete')) parts.push(el('p', {className: 'repair-turn-note'}, INCOMPLETE_TEXT));
    if (meta.status === 'awaiting_continue') parts.push(el('p', {className: 'repair-turn-note'}, 'The AI is waiting for you. Continue sends its next request.'), action('Continue', () => sendTurn({continueTurn: true, message: turnMessage, repairSnapshot: meta.repairSnapshot}), true));
    else if (meta.status === 'interrupted') parts.push(el('p', {className: 'repair-turn-note'}, 'This turn stopped before it finished. Nothing was applied.'), retryButton({message: turnMessage, repairSnapshot: meta.repairSnapshot}));
    return parts.length ? el('div', {className: 'repair-turn-footer'}, parts) : null;
  };
  const clockTime = () => new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  const appendMessage = (role, text, meta = null, turnMessage = '', live = false) => {
    transcript.querySelector('.assistant-welcome')?.remove();
    const detail = role === 'user' ? (live ? MODES.find(item => item.value === mode)?.label : null) : (live ? selectedProvider()?.model : null);
    const node = el('article', {className: `chat-message chat-${role}${live ? ' is-new' : ''}`},
      el('div', {className: 'chat-meta'}, el('strong', {}, role === 'user' ? 'You' : 'Assistant'), detail ? el('span', {}, detail) : null, live ? el('time', {}, clockTime()) : null),
      text ? el('p', {className: 'pre-wrap'}, text) : null);
    if (role === 'assistant' && meta) {
      transcript.querySelectorAll('.repair-turn-footer .button').forEach(button => button.remove());
      const ids = actionIds(meta);
      if (ids.length) node.dataset.actionIds = ids.join(' ');
      if (Array.isArray(meta.steps) && meta.steps.length) node.append(turnSteps(meta.steps));
      if (Array.isArray(meta.context_log) && meta.context_log.length) node.append(contextSent(meta.context_log));
      const footer = turnFooter(meta, turnMessage);
      if (footer) node.append(footer);
    }
    if (role !== 'assistant' && Array.isArray(meta?.context_log) && meta.context_log.length) node.append(contextSent(meta.context_log));
    transcript.append(node);
    return node;
  };
  const placeProposals = async (node, meta) => {
    const pending = Array.isArray(meta?.pending_actions) ? meta.pending_actions : [];
    let anchor = node;
    for (const id of actionIds(meta).slice(0, 20)) {
      let item = pending.find(entry => entry?.id === id);
      if (!item || (isRepairAction(item) && !item.preview)) item = (await fetchAction(id)) || item;
      if (signal.aborted) return;
      if (item && isRepairAction(item)) anchor = mountProposal(item, anchor).card;
    }
  };
  const chatFailure = (error, turn) => {
    if (error.code === 'repair_error_text_disabled' || error.code === 'repair_webhook_directory_disabled') {
      const errorText = error.code === 'repair_error_text_disabled';
      if (errorText) applyErrorTextSetting(false); else resetContext(true);
      // The server dropped the paused turn, so Try again starts a new turn with the same message instead of continuing.
      messages.append(el('div', {className: 'repair-turn-retry', role: 'status'},
        el('p', {}, errorText ? `${ERROR_TEXT_OFF} Try again to send it without the error text.` : `${DIRECTORY_OFF} Try again to send it without them.`),
        retryButton({message: turn.message, continueTurn: false})));
      return;
    }
    const limited = error.code === 'provider_rate_limited' || (error.status === 429 && error.code !== 'assistant_rate_limit');
    if (limited || error.code === 'provider_incomplete') {
      messages.append(el('div', {className: 'repair-turn-retry', role: 'status'}, el('p', {}, limited ? RATE_LIMITED_TEXT : INCOMPLETE_TEXT), retryButton(turn)));
      return;
    }
    notice(messages, error); messages.append(el('p', {}, 'No automatic retry was sent. Refresh proposals before trying again; the provider may have created a pending action.'));
  };
  const sendTurn = async ({message = '', continueTurn = false, retry = false, repairSnapshot} = {}) => {
    if (busy || !provider.value || (!continueTurn && !message.trim())) return false;
    busy = true; send.disabled = true; provider.disabled = true;
    announce(continueTurn ? 'Continuing the turn. Waiting for the assistant.' : 'Message sent. Waiting for the assistant.');
    messages.replaceChildren();
    if (!continueTurn && !retry) appendMessage('user', message, null, '', true);
    // Continue uses the context of the paused turn, even if the next-message note has been edited.
    const sentContext = repairSnapshot === undefined ? (target ? repairContext(target, errorTextEnabled(), operatorNotes.value) : null) : repairSnapshot;
    const body = {provider_id: provider.value, ...(chatId ? {chat_id: chatId} : {}), ...(sentContext ? {context: sentContext} : {})};
    if (continueTurn) body.continue_turn = true; else body.message = message;
    body.allow_changes = allowChanges();
    try {
      const result = await request('/chat', {method: 'POST', json: body});
      if (signal.aborted) return false;
      chatId = result?.id || result?.chat_id || chatId;
      rememberChat(chatId, message || lastMessage);
      resumeId.value = chatId || '';
      if (!continueTurn) lastMessage = message;
      if (input.value === message) { input.value = ''; scheduleComposerSize(); }
      const node = appendMessage('assistant', result?.content ?? '', {...(result && typeof result === 'object' ? result : {}), repairSnapshot: sentContext}, message || lastMessage, true);
      await placeProposals(node, result);
      await refreshApprovals().catch(error => { if (!signal.aborted) notice(messages, error); });
      const answer = typeof result?.content === 'string' ? result.content.trim().replace(/\s+/g, ' ') : '';
      const steps = Array.isArray(result?.steps) ? result.steps : [];
      const done = steps.filter(step => step.status === 'done').length;
      const failed = steps.filter(step => ['failed','rate_limited'].includes(step.status)).length;
      const pending = Array.isArray(result?.pending_actions) ? result.pending_actions.filter(item => item.status === 'pending').length : 0;
      announce([answer ? `Assistant: ${answer.slice(0, 200)}${answer.length > 200 ? '… Full response in the conversation.' : ''}` : 'Assistant turn complete.',
        done ? `${done} step${done === 1 ? '' : 's'} completed.` : '', failed ? `${failed} step${failed === 1 ? '' : 's'} did not complete.` : '',
        pending ? `${pending} proposal${pending === 1 ? '' : 's'} awaiting your review. Nothing was applied.` : '',
        result?.status === 'awaiting_continue' ? 'Continue when you are ready.' : result?.status === 'interrupted' ? 'The turn stopped before it finished.' : ''].filter(Boolean).join(' '));
      return true;
    } catch (error) {
      if (!signal.aborted) chatFailure(error, {message: message || lastMessage, continueTurn, repairSnapshot: sentContext});
      return false;
    } finally { busy = false; if (!signal.aborted) { send.disabled = !modelsAvailable; provider.disabled = !modelsAvailable; } }
  };
  const send = el('button', {type: 'submit', className: 'composer-send', disabled: true, 'aria-label': 'Send message', title: 'Send (Enter)'}, icon('send'));
  // Ask is the default: the server offers the model no tool that could create an approval,
  // so a question never comes back as a proposal, whatever the model decides to do.
  // Create workflow plans a new inactive workflow on one instance; it is Propose with that instance pinned.
  const PLACEHOLDERS = {ask: 'Ask about an execution or workflow…', propose: 'Describe what to fix…', create: 'Describe the workflow to create…'};
  const modeMenu = selectMenu({label: 'Mode', options: MODES, value: 'ask', className: 'composer-select', onChange: value => setMode(value, true)});
  const allowChanges = () => mode !== 'ask';
  const creationPick = selectMenu({label: 'Instance for the new workflow', className: 'composer-select',
    options: [{value: '', label: 'Choose an instance'}, ...state.instances.map(row => ({value: row.id, label: row.name || row.id, glyph: 'instances'}))],
    onChange: value => { if (value) beginCreation(value); }});
  const creationRow = el('div', {className: 'composer-create', hidden: true}, el('span', {}, 'New workflow on'), creationPick.node);
  const beginCreation = instanceId => {
    setTarget({mode: 'repair', instance_id: instanceId}, true);
    creationRow.hidden = true;
    if (!input.value.trim()) input.value = 'Propose a new inactive workflow for this instance. ';
    scheduleComposerSize(); input.focus();
  };
  const startCreation = () => {
    if (!state.instances.length) { notice(messages, 'Connect an instance before creating a workflow.'); setMode('ask'); return; }
    if (state.instanceId) { beginCreation(state.instanceId); return; }
    creationPick.value = '';
    creationRow.hidden = false; creationPick.button.focus();
  };
  const setMode = (next, chosen = false) => {
    mode = MODES.some(item => item.value === next) ? next : 'ask';
    modeMenu.value = mode;
    input.placeholder = PLACEHOLDERS[mode];
    if (mode !== 'create') creationRow.hidden = true;
    paintToolSupport();
    if (chosen && mode === 'create') startCreation();
  };
  input.id = 'assistant-message';
  const form = el('form', {className: 'assistant-composer'}, creationRow,
    el('label', {className: 'sr-only', for: 'assistant-message'}, 'Your message'), input, composerWarning,
    el('div', {className: 'composer-foot'}, el('div', {className: 'composer-controls'}, modeMenu.node, provider.node),
      el('span', {className: 'composer-hint'}, 'Enter to send · Shift+Enter for a new line'), send));
  // Enter sends; Shift+Enter (and IME composition) keeps typing.
  listen(input, 'keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); form.requestSubmit(); } });
  setMode('ask');
  // Follow chat's end, not the document's end: the action rail follows the chat.
  // Detect the overflow owner so expanded repair layouts use the same policy as
  // desktop. Instant scrolling also respects prefers-reduced-motion.
  let following = false, revealingComposer = false;
  const scrollGeometry = () => {
    const nested = /auto|scroll/.test(getComputedStyle(transcript).overflowY);
    const viewport = window.visualViewport;
    const owner = nested ? transcript : window;
    const remaining = nested ? transcript.scrollHeight - transcript.clientHeight - transcript.scrollTop
      : form.getBoundingClientRect().bottom - ((viewport?.offsetTop || 0) + (viewport?.height || document.documentElement.clientHeight) - 8);
    return {owner, remaining};
  };
  const followConversation = () => {
    if (!following || signal.aborted || !form.isConnected || (window.visualViewport?.scale || 1) !== 1) return;
    const {owner, remaining} = scrollGeometry();
    if (Math.abs(remaining) > 1) owner.scrollBy({top: remaining, behavior: 'instant'});
  };
  const trackScroll = () => {
    // Focus can scroll before the queued keyboard/composer resize has settled.
    if (revealingComposer) return;
    following = Math.abs(scrollGeometry().remaining) <= 32;
  };
  listen(window, 'scroll', trackScroll);
  listen(transcript, 'scroll', trackScroll);
  // Mutation delivery precedes scroll events. Include async proposal insertion
  // without confusing content growth with the operator scrolling into history.
  const conversationObserver = new MutationObserver(followConversation);
  conversationObserver.observe(transcript, {childList: true, subtree: true, characterData: true});
  conversationObserver.observe(messages, {childList: true, subtree: true, characterData: true});
  // iOS keeps its layout viewport when the keyboard opens. Size and reveal the whole
  // composer against the visual viewport, leaving at least 65% for the conversation.
  let composerFrame = 0;
  const resizeComposer = () => {
    composerFrame = 0;
    revealingComposer = false;
    if (signal.aborted) return;
    // On larger screens the message box grows with its text, from one line to about eight.
    if (!matchMedia('(max-width:700px)').matches) {
      input.style.height = 'auto';
      const border = input.offsetHeight - input.clientHeight, height = Math.min(Math.max(input.scrollHeight + border, 48), 200);
      input.style.height = `${height}px`; input.style.overflowY = input.scrollHeight + border > 200 ? 'auto' : 'hidden';
      followConversation(); return;
    }
    const viewport = window.visualViewport;
    const visibleHeight = viewport?.height || document.documentElement.clientHeight;
    input.style.height = '44px';
    const border = input.offsetHeight - input.clientHeight;
    const chrome = form.getBoundingClientRect().height - input.getBoundingClientRect().height;
    const limit = Math.max(44, Math.min(144, visibleHeight * .35 - chrome));
    const height = Math.max(44, Math.min(input.scrollHeight + border, limit));
    input.style.height = `${height}px`;
    input.style.overflowY = input.scrollHeight + border > height ? 'auto' : 'hidden';
    followConversation();
  };
  const scheduleComposerSize = () => { if (!composerFrame) composerFrame = requestAnimationFrame(resizeComposer); };
  listen(input, 'input', scheduleComposerSize);
  listen(input, 'focus', () => {
    const {owner, remaining} = scrollGeometry();
    // Focusing the page's composer explicitly returns to it; on desktop the
    // composer is already visible, so keep the transcript's history position.
    following = owner === window || remaining <= 32;
    revealingComposer = true;
    scheduleComposerSize();
  });
  listen(window, 'resize', scheduleComposerSize);
  if (window.visualViewport) {
    listen(window.visualViewport, 'resize', scheduleComposerSize);
    listen(window.visualViewport, 'scroll', scheduleComposerSize);
  }
  listen(form, 'submit', async event => {
    event.preventDefault();
    if (busy || !input.value.trim() || !provider.value) return;
    await sendTurn({message: input.value});
  });
  const newChat = action('New conversation', () => { if (busy) return; chatId = null; input.value = ''; scheduleComposerSize(); resumeId.value = ''; showWelcome(); });
  const resumeId = el('input', {type: 'text', maxLength: 64, placeholder: 'Conversation ID', 'aria-label': 'Conversation ID'});
  // Recently opened conversations in this browser (ids only; transcripts stay server side).
  // There is no list endpoint, so this covers what this browser has seen; pasting an ID still works.
  const recentKey = `inference:recent-chats:${state.user?.username || 'anonymous'}`;
  function readRecents() {
    try {
      const rows = JSON.parse(localStorage.getItem(recentKey) ?? '[]');
      if (!Array.isArray(rows)) throw new Error('unreadable');
      return rows.filter(row => row && typeof row.id === 'string');
    } catch { return null; }
  }
  function rememberChat(id, title) {
    if (!id) return;
    const rows = readRecents() || [];
    try { localStorage.setItem(recentKey, JSON.stringify([{id, title: String(title || id).slice(0, 120), ts: Date.now()}, ...rows.filter(row => row.id !== id)].slice(0, 20))); } catch {}
    recentPicker.reload();
  }
  const recentPicker = createIdPicker({label: 'Find a conversation', placeholder: 'Filter by title',
    load: async () => {
      const rows = readRecents();
      if (rows === null) throw new Error('Saved conversations could not be read from this browser.');
      return rows.map(row => ({id: row.id, name: row.title || row.id, detail: Number.isFinite(row.ts) ? new Date(row.ts).toLocaleString() : ''}));
    },
    emptyText: 'No saved conversations in this browser yet. Paste an ID below to open one.',
    clearLabel: 'Clear selection',
    onPick: id => { if (id) openConversation(id).catch(error => { if (!signal.aborted) notice(messages, error); }); }, signal});
  const openConversation = async id => {
    const result = await request(`/chats/${encodeURIComponent(id)}`);
    if (signal.aborted) return;
    chatId = result.id; resumeId.value = chatId; transcript.replaceChildren(); messages.replaceChildren();
    let turnMessage = '';
    for (const item of Array.isArray(result.data) ? result.data : []) {
      if (item?.role === 'user') { turnMessage = item.content; appendMessage('user', item.content, item); continue; }
      const node = appendMessage('assistant', item?.content ?? '', item && typeof item === 'object' ? item : {}, turnMessage);
      await placeProposals(node, item);
      if (signal.aborted) return;
    }
    lastMessage = turnMessage;
    rememberChat(chatId, turnMessage);
    await refreshApprovals();
  };
  const resume = action('Open conversation', async () => {
    if (busy || !resumeId.value.trim()) return;
    await openConversation(resumeId.value.trim());
  });
  const showInConversation = async row => {
    let model = proposals.get(row.id);
    if (!model?.card.isConnected && row.chat_id && row.chat_id !== chatId && !busy) { await openConversation(row.chat_id); model = proposals.get(row.id); }
    if (signal.aborted) return;
    if (!model?.card.isConnected) {
      const item = await fetchAction(row.id);
      if (signal.aborted) return;
      if (!item) throw new Error('This proposal could not be loaded. Refresh proposals and try again.');
      model = mountProposal({...item, status: row.status || item.status});
    }
    model.card.scrollIntoView({block: 'nearest'});
    model.card.focus({preventScroll: true});
  };
  function makeDrawer(id, title, meta, body, className) {
    const close = el('button', {type: 'button', className: 'peek-close', 'aria-label': `Close ${title.toLowerCase()}`}, icon('close'));
    const dialog = el('dialog', {className: `peek side-drawer ${className}`, 'aria-labelledby': id},
      el('header', {className: 'peek-head'}, el('div', {className: 'peek-heading'}, el('h2', {className: 'peek-title', id}, title),
        meta ? el('p', {className: 'peek-meta'}, meta) : null), close),
      el('div', {className: 'peek-body'}, body));
    listen(close, 'click', () => dialog.close());
    listen(dialog, 'click', event => { if (event.target !== dialog) return; const box = dialog.getBoundingClientRect();
      if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close(); });
    listen(dialog, 'keydown', event => event.stopPropagation());
    return dialog;
  }
  // What leaves this installation, said once under the message box.
  const privacy=el('p',{className:'composer-privacy'},icon('shield'),el('span',{},'Messages go to the selected model provider. Execution inputs, outputs and stored secrets are not sent.'));
  const chat=el('section',{className:'assistant-chat','aria-label':'Chat workspace'},repairBar,transcript,messages,form,privacy);
  // Saved conversations open from the header, like a conversation switcher.
  const savedChats=el('details',{className:'assistant-history'},el('summary',{className:'button button-ghost'},icon('history'),'Saved conversations'),
    el('div',{className:'assistant-history-panel'},recentPicker.node,field('Conversation ID',resumeId),resume));
  listen(savedChats,'keydown',event=>{if(event.key==='Escape'&&savedChats.open){event.stopPropagation();savedChats.open=false;savedChats.querySelector('summary').focus();}});
  listen(document,'click',event=>{if(savedChats.open&&!savedChats.contains(event.target))savedChats.open=false;});
  // Review leads the rail: it is what needs a person. The count and the last refresh replace a button wall.
  const reviewCount=el('span',{className:'review-count'});
  const reviewUpdated=el('span',{className:'review-updated'});
  const refreshReview=el('button',{type:'button',className:'icon-button','aria-label':'Refresh proposals',title:'Refresh proposals'},icon('refresh'));
  listen(refreshReview,'click',async()=>{refreshReview.disabled=true;try{await refreshApprovals();}catch(error){if(!signal.aborted)notice(messages,error);}finally{if(!signal.aborted)refreshReview.disabled=false;}});
  // Proposals already appear in the conversation, so review is not a second permanent box: it is a button
  // with the pending count, and the full list (including actions from other conversations) opens beside it.
  const reviewDrawer=makeDrawer('assistant-review-title','Review','Changes the AI proposed. Nothing runs until you apply it.',
    el('div',{className:'assistant-approval-panel'},el('div',{className:'assistant-rail-heading'},reviewUpdated,refreshReview),approvals),'review-drawer');
  const reviewButton=el('button',{type:'button',className:'button button-ghost review-button'},icon('check'),'Review',reviewCount);
  listen(reviewButton,'click',()=>{if(!reviewDrawer.open)reviewDrawer.showModal();refreshApprovals().catch(error=>{if(!signal.aborted)notice(messages,error);});});
  container.replaceChildren(polite,urgent,el('div',{className:'page-heading-row assistant-head'},heading('AI workspace','Ask about executions and workflows. Every change waits for your approval.'),
    el('div',{className:'actions'},reviewButton,savedChats,newChat,can('admin')?action('Assistant settings',()=>openSettings()):null)),el('div',{className:'assistant-layout'},chat),reviewDrawer);
  transcript.append(el('p',{className:'muted',role:'status'},'Loading assistant…'));
  paintTarget();
  if (parsed.rejected.length) notice(messages, `This repair link had invalid values, which were ignored: ${parsed.rejected.join(', ')}.`);
  const context = new URLSearchParams(location.hash.split('?')[1] || '');
  const errorId = context.get('error_id');
  const repairPrompt = !target ? '' : target.error_id ? `Find the cause of error ${target.error_id} in the pinned workflow and propose a fix I can review.`
    : target.execution_id ? `Find why execution ${target.execution_id} failed and propose a fix I can review.` : target.workflow_id ? 'Check the pinned workflow for problems and propose a fix I can review.' : 'Propose a new inactive workflow for this instance. ';
  const explain = context.get('explain') || repairPrompt || (errorId ? `Help me investigate error ${errorId.slice(0,128)} for instance ${(context.get('instance_id') || state.instanceId).slice(0,128)}. Use available metadata and suggest next steps.` : '');
  if (explain) input.value = explain.slice(0, 16000);
  // Arriving from "Repair with AI" is a request for a fix; everything else starts as a question.
  if (repairPrompt && !context.get('explain')) setMode('propose');
  scheduleComposerSize();

  let repairSettingsReady = false, repairSettingsError = null, directoryAllowed = true, shareDirectory = null;
  const loadRepairSettings = async () => {
    try {
      const result = await request('/repair/settings');
      if (signal.aborted) return;
      if (typeof result?.error_text_enabled === 'boolean') applyErrorTextSetting(result.error_text_enabled);
      if (typeof result?.webhook_directory_enabled === 'boolean') directoryAllowed = result.webhook_directory_enabled;
      repairSettingsReady = true;
      if (allowErrorText) allowErrorText.disabled = false;
      if (shareDirectory) { shareDirectory.checked = directoryAllowed; shareDirectory.disabled = false; }
    } catch (error) {
      if (signal.aborted) return;
      repairSettingsError = error;
      if (repairFeedback) notice(repairFeedback, error);
    }
  };
  if (can('admin')) {
    // Models, remote tools and repair switches in one drawer: configured here, chosen in the composer.
    settings = el('div', {className: 'assistant-settings'});
    const drawer = makeDrawer('assistant-settings-title', 'Assistant settings', 'Models, remote tools and what repair conversations may send.', settings, 'settings-drawer');
    const modelList = el('ul', {className: 'saved-models'}), modelFeedback = el('div', {'aria-live': 'polite'});
    let addingModel = false;
    const addModel = el('button', {type: 'button', className: 'button button-ghost'}, actionIcon('Add connection'), 'Add connection');
    const modelsSection = el('section', {className: 'drawer-section', hidden: true},
      el('div', {className: 'saved-models-head'}, el('h3', {}, 'Models'), addModel),
      el('p', {className: 'muted'}, 'A model must pass the tool-calling check to propose changes. Each check sends one short request to the provider.'),
      modelList, modelFeedback);
    settings.append(modelsSection);
    const setupForm = providerSetup(settings, refreshProviders, signal);
    const showForm = () => { addingModel = true; setupForm.hidden = false; addModel.hidden = true; setupForm.querySelector('input')?.focus(); };
    listen(addModel, 'click', showForm);
    listen(setupForm, 'input', () => { addingModel = true; });
    const cancelModel = el('button', {type: 'button', className: 'button button-ghost'}, 'Cancel');
    setupForm.querySelector('.form-actions')?.prepend(cancelModel);
    listen(cancelModel, 'click', () => {
      addingModel = false; setupForm.reset();
      setupForm.querySelector('select')?.dispatchEvent(new Event('change'));
      paintModels(); addModel.focus();
    });
    const rowAction = (label, name, handler) => {
      const node = el('button', {type: 'button', className: 'button button-ghost', 'aria-label': `${label} for ${name}`}, label);
      listen(node, 'click', async () => {
        node.disabled = true;
        try { await handler(); } catch (error) { if (!signal.aborted) notice(modelFeedback, error); }
        finally { if (!signal.aborted) node.disabled = false; }
      });
      return node;
    };
    const modelRow = row => el('li', {className: 'saved-model', 'data-id': row.id},
      el('div', {className: 'saved-model-name'}, el('strong', {}, row.id), el('span', {className: 'muted'}, [providerLabel(row.kind), row.model].filter(Boolean).join(' · '))),
      toolBadge(row),
      el('div', {className: 'saved-model-actions'},
        rowAction('Test connection', row.id, async () => {
          modelFeedback.replaceChildren();
          const result = await request(`/providers/${encodeURIComponent(row.id)}/test`, {method: 'POST'});
          if (!signal.aborted) notice(modelFeedback, `Connected to ${result?.model || row.model}.`);
        }),
        rowAction('Check tool calling', row.id, () => checkToolCalling(row, modelFeedback))));
    // A check updates its own row in place, so focus stays on the button that ran it.
    paintModels = changed => {
      if (changed) { modelList.querySelector(`[data-id="${CSS.escape(changed.id)}"] .tool-calling-badge`)?.replaceWith(toolBadge(changed)); return; }
      modelsSection.hidden = !providerRows.length;
      const formShown = !providerRows.length || addingModel;
      setupForm.hidden = !formShown; addModel.hidden = formShown; cancelModel.hidden = !providerRows.length;
      modelList.replaceChildren(...providerRows.map(modelRow));
    };
    paintModels();
    openSettings = () => {
      if (!drawer.open) drawer.showModal();
      if (!allowErrorText) renderRepairSettings();
      (setupForm.hidden ? addModel : setupForm.querySelector('input'))?.focus();
    };
    const serverId = el('input', {required: true, pattern: '[A-Za-z0-9_\\-]{1,64}', maxLength: 64});
    const url = el('input', {type: 'url', required: true});
    const mcpKey = el('input', {placeholder: '$MCP_KEY', autocomplete: 'off'});
    const mcpSave = el('button', {type: 'submit', className: 'button button-primary'}, 'Save MCP server');
    const mcpCancel = el('button', {type: 'button', className: 'button button-ghost'}, 'Cancel');
    const mcpFeedback = el('div', {'aria-live': 'polite'});
    const mcpForm = formCard({title: 'Add an MCP server', heading: 'h3', flat: true,
      body: [el('div', {className: 'form-row'}, field('Server ID', serverId, {hint: 'Letters, numbers, dashes and underscores.'}),
        field('Streamable HTTP endpoint', url, {})),
        field('Key reference (optional)', mcpKey, {hint: 'Optional.'})],
      actions: [mcpCancel, mcpSave]});
    const catalogs = el('div', {className: 'stack'});
    const loadServers = async () => {
      const result = await request('/config/mcp');
      if (signal.aborted) return;
      catalogs.replaceChildren(...result.data.map(server => {
        const output = el('div');
        return el('div', {className: 'mcp-server'}, el('div', {className: 'mcp-server-head'}, el('h4', {}, server.id), el('span', {className: 'muted'}, server.url)), action('Discover tools', async () => {
          const catalog = await request(`/mcp/${encodeURIComponent(server.id)}/tools`);
          if (signal.aborted) return;
          // Tool names and descriptions as a list; the full schema stays behind a disclosure.
          const tools = Array.isArray(catalog?.data) ? catalog.data : Array.isArray(catalog?.data?.tools) ? catalog.data.tools : [];
          output.replaceChildren(tools.length ? el('ul', {className: 'mcp-tools'}, tools.map(tool => el('li', {}, el('strong', {}, String(tool?.name || 'Unnamed tool')),
            tool?.description ? el('span', {}, String(tool.description)) : null))) : el('p', {className: 'muted'}, 'This server lists no tools.'), rawDisclosure(catalog.data, 'Tool schemas'));
        }), output);
      }));
    };
    listen(mcpForm, 'submit', async event => {
      event.preventDefault(); mcpSave.disabled = true;
      mcpFeedback.replaceChildren();
      try {
        await request('/config/mcp', {method: 'PUT', json: {id: serverId.value, config: {url: url.value, api_key_ref: mcpKey.value || null}}});
        const saved = serverId.value; await loadServers(); closeServerForm();
        if (!signal.aborted) notice(mcpFeedback, `MCP server ${saved} saved. Discover its tools below.`);
      }
      catch (error) { if (!signal.aborted) notice(mcpFeedback, error); }
      finally { mcpSave.disabled = false; }
    });
    const addServer = el('button', {type: 'button', className: 'button button-ghost'}, actionIcon('Add server'), 'Add server');
    mcpForm.hidden = true;
    // Cancel closes the form and forgets what was typed; saving closes it once the server is listed.
    const closeServerForm = () => { mcpForm.hidden = true; addServer.hidden = false; mcpForm.reset(); };
    listen(addServer, 'click', () => { mcpFeedback.replaceChildren(); mcpForm.hidden = false; addServer.hidden = true; serverId.focus(); });
    listen(mcpCancel, 'click', () => { closeServerForm(); addServer.focus(); });
    settings.append(el('section', {className: 'drawer-section'},
      el('div', {className: 'saved-models-head'}, el('h3', {}, 'Remote MCP tools'), addServer),
      el('p', {className: 'muted'}, 'Optional. Add a server to let the assistant propose its tools; every MCP call waits for your confirmation.'),
      catalogs, mcpFeedback, mcpForm));
    // Repair conversations: installation-wide switches for what repair turns may send to providers.
    // Built when settings first open, after the provider form, so the controls only exist where they can be seen.
    const renderRepairSettings = () => {
      allowErrorText = el('input', {type: 'checkbox', checked: errorTextAllowed, disabled: !repairSettingsReady});
      shareDirectory = el('input', {type: 'checkbox', checked: directoryAllowed, disabled: !repairSettingsReady});
      repairFeedback = el('div', {'aria-live': 'polite'});
      listen(shareDirectory, 'change', async () => {
        const value = shareDirectory.checked, changed = value !== directoryAllowed;
        shareDirectory.disabled = true; repairFeedback.replaceChildren();
        try {
          const saved = await request('/repair/settings', {method: 'PUT', json: {webhook_directory_enabled: value}});
          if (signal.aborted) return;
          directoryAllowed = typeof saved?.webhook_directory_enabled === 'boolean' ? saved.webhook_directory_enabled : value;
          shareDirectory.checked = directoryAllowed;
          if (changed) resetContext(true);
          notice(repairFeedback, 'Repair setting saved.');
        } catch (error) { if (!signal.aborted) { shareDirectory.checked = !value; notice(repairFeedback, error); } }
        finally { if (!signal.aborted) shareDirectory.disabled = false; }
      });
      listen(allowErrorText, 'change', async () => {
        const value = allowErrorText.checked, changed = value !== errorTextAllowed;
        allowErrorText.disabled = true; repairFeedback.replaceChildren();
        try {
          const saved = await request('/repair/settings', {method: 'PUT', json: {error_text_enabled: value}});
          if (signal.aborted) return;
          applyErrorTextSetting(typeof saved?.error_text_enabled === 'boolean' ? saved.error_text_enabled : value);
          // The context shown under What the AI sees no longer applies; it reloads when reopened.
          if (changed) resetContext(true);
          notice(repairFeedback, 'Repair setting saved.');
        } catch (error) { if (!signal.aborted) { allowErrorText.checked = !value; notice(repairFeedback, error); } }
        finally { if (!signal.aborted) allowErrorText.disabled = false; }
      });
      settings.append(el('h3', {}, 'Repair conversations'), el('label', {className: 'repair-check'}, allowErrorText, el('span', {}, 'Allow error text in repair conversations')),
        el('p', {className: 'muted'}, 'When on, people can choose to send the scrubbed error message from a failed execution to the model. Secret values stay masked either way.'),
        el('label', {className: 'repair-check'}, shareDirectory, el('span', {}, 'Share other workflows’ trigger addresses with the AI')),
        el('p', {className: 'muted'}, 'When on, repair context lists the names and trigger addresses of other active workflows on the same instance, so the AI can point a node at the right one instead of guessing. Nothing else from those workflows is sent.'), repairFeedback);
      if (repairSettingsError) notice(repairFeedback, repairSettingsError);
    };
    container.append(drawer);
    loadServers().catch(error => { if (!signal.aborted) notice(messages, error); });
  }
  await Promise.allSettled([refreshProviders(), refreshApprovals(), loadRepairSettings()]).then(results => {
    if (!signal.aborted) results.slice(0, 2).forEach((result,index)=>{
      if(result.status !== 'rejected')return;
      notice(messages,result.reason);
      if(index===0){provider.setOptions([{value:'',label:'Models unavailable'}]);transcript.replaceChildren(el('div',{className:'assistant-welcome'},el('h2',{},'Could not load models'),el('p',{},'Refresh the page to try again.')));}
    });
  });
  const timer = setInterval(() => {
    if (signal.aborted) return;
    if (approvalRows.some(row => row.status === 'pending')) paintApprovals();
    // Repaint a card only when its visible status changes, so focus and open disclosures stay put.
    for (const model of proposals.values()) if (model.card.isConnected && model.painted !== effectiveStatus({...model, acting: acting.has(model.action.id)})) paintProposal(model);
  }, 15000);
  return () => { controller.abort(); clearInterval(timer); clearTimeout(notesTimer); cancelAnimationFrame(composerFrame); conversationObserver.disconnect(); };
}
