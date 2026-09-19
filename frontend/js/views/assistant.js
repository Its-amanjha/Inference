import {providerSetup} from '../lib/provider-setup.js';
import {api} from '../api.js';
import {can, state} from '../state.js';
import {el, field, formCard, heading, jsonView, notice} from '../lib/html.js';
import {icon,actionIcon} from '../lib/icons.js';
import {rawDisclosure, instanceName, toneBadge, formatTime} from '../lib/payload.js';
import {confirmAction} from '../lib/confirm.js';
import {parseRepairTarget, repairHash, repairContext, contextQuery, describeTarget, listWorkflows, targetChip, createIdPicker} from '../lib/repair-target.js';
import {isRepairAction, proposalShell, renderProposal, effectiveStatus, statusBadge, statusForError, preconditionsOf, preconditionKey,
  turnSteps, contextSent, contextOverview, RATE_LIMITED_TEXT, INCOMPLETE_TEXT} from '../lib/proposals.js';

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
  const messages = el('div', {className:'assistant-notices','aria-live': 'polite'});
  const provider = el('select', {required: true, disabled:true, 'aria-label': 'Assistant provider'},el('option',{value:''},'Loading models…'));
  const transcript = el('div', {className: 'chat-transcript', role: 'log', 'aria-label': 'Conversation', 'aria-live': 'polite'});
  const input = el('textarea', {rows: 3, maxLength: 16000, required: true, disabled:true, placeholder: 'Ask about an execution or workflow…'});
  const approvals = el('div', {className: 'stack'});
  let chatId = null;
  let busy = false;
  let approvalRows = [];
  let providerRows = [];
  let modelsAvailable = false;
  let settings = null;
  let lastMessage = '';
  const openSettings = () => { if(settings){settings.open=true;settings.scrollIntoView({block:'start'});settings.querySelector('input')?.focus({preventScroll:true});} };
  const acting = new Set();
  const proposals = new Map();
  const loadingActions = new Set();
  const workflowNames = new Map();
  const showWelcome = () => {
    const welcome=el('div',{className:'assistant-welcome'},icon('assistant'),el('h2',{},modelsAvailable?'What would you like to investigate?':'Connect a model to get started'),
      el('p',{},modelsAvailable?'Ask about a failed execution, inspect a workflow, or review the next action.':can('admin')?'Choose a local or hosted model in Assistant settings.':'Ask your administrator to connect a model.'));
    if(!modelsAvailable&&can('admin'))welcome.append(action('Connect model',openSettings,true));
    transcript.replaceChildren(welcome);
  };

  // Repair target, "What the AI sees" and model support. Only people pin or change the target.
  const parsed = parseRepairTarget();
  let target = parsed.target;
  let errorTextAllowed = true;
  let allowErrorText = null;
  let repairFeedback = null;
  const sendErrorText = el('input', {type: 'checkbox', checked: true});
  const errorTextNote = el('p', {className: 'repair-admin-note', hidden: true}, ERROR_TEXT_OFF);
  const contextBody = el('div', {className: 'repair-context-body'});
  const contextDetails = el('details', {className: 'repair-context'}, el('summary', {}, 'What the AI sees'),
    el('div', {className: 'repair-context-panel'}, el('label', {className: 'repair-check'}, sendErrorText, el('span', {}, 'Send scrubbed error text to the AI')), errorTextNote, contextBody));
  const modelNotice = el('div', {className: 'repair-model-notice', role: 'status', hidden: true});
  const chip = targetChip({signal, onChange: next => setTarget(next, true)});
  const repairBar = el('div', {className: 'repair-bar', hidden: true}, chip.node, contextDetails, modelNotice);
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
    const query = contextQuery(target, errorTextEnabled()).toString();
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

  const selectedProvider = () => providerRows.find(row => row.id === provider.value);
  const toolBadge = el('span', {className: 'badge tool-calling-badge', hidden: true});
  const toolCheck = can('admin') ? action('Check tool calling', () => checkToolCalling()) : null;
  if (toolCheck) toolCheck.hidden = true;
  const paintToolSupport = () => {
    const row = selectedProvider();
    const reported = row?.tool_calling?.status;
    const status = Object.hasOwn(TOOL_SUPPORT, reported ?? '') ? reported : 'untested';
    const [label, tone] = TOOL_SUPPORT[status];
    toolBadge.hidden = !row;
    toolBadge.className = `badge tool-calling-badge${tone ? ` ${tone}` : ''}`;
    toolBadge.dataset.status = status;
    toolBadge.textContent = label;
    toolBadge.title = row?.tool_calling?.checked_at ? `Checked ${formatTime(row.tool_calling.checked_at)}` : 'Not checked yet';
    if (toolCheck) toolCheck.hidden = !row;
    // Diagnosis stays available either way; the notice only sets expectations about proposals.
    const show = Boolean(target && row && status !== 'supported');
    modelNotice.hidden = !show;
    modelNotice.replaceChildren(...(show ? [icon('info'), el('p', {}, status === 'unsupported' ? UNSUPPORTED_TEXT : UNTESTED_TEXT)] : []));
  };
  const checkToolCalling = async () => {
    const row = selectedProvider();
    if (!row) return;
    messages.replaceChildren();
    let result;
    try { result = await request(`/providers/${encodeURIComponent(row.id)}/tool-test`, {method: 'POST'}); }
    catch (error) { if (error.code !== 'provider_rate_limited') throw error; if (!signal.aborted) notice(messages, RATE_LIMITED_TEXT); return; }
    if (signal.aborted) return;
    // The result may be nested under tool_calling or sent at the top level; a failed check never changes the stored result.
    const checked = result?.tool_calling && typeof result.tool_calling === 'object' ? result.tool_calling : result;
    const status = typeof checked?.status === 'string' ? checked.status : 'untested';
    row.tool_calling = {status, checked_at: checked?.checked_at ?? null};
    paintToolSupport();
    const summary = {supported: 'Tool calling works with this model, so it can propose repairs.', unsupported: 'This model did not pass the tool-calling check, so it cannot propose repairs.',
      untested: 'The tool-calling check did not finish; this model is still untested.'}[status] || `The tool-calling check returned ${status}.`;
    notice(messages, result?.detail ? `${summary} ${result.detail}` : summary);
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
      transcript.append(note); transcript.scrollTop = transcript.scrollHeight;
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
    provider.replaceChildren(...providerRows.map(item => el('option', {value: item.id}, `${item.model} · ${item.id}`)));
    if(!modelsAvailable)provider.append(el('option',{value:''},'No model connected'));
    if (providerRows.some(item => item.id === selected)) provider.value = selected;
    send.disabled = !modelsAvailable||busy;
    input.disabled = !modelsAvailable;
    provider.disabled = !modelsAvailable||busy;
    paintToolSupport();
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

  // Inline repair proposals: one card per action, painted in place wherever it sits in the conversation.
  const fetchAction = async id => {
    try {
      const result = await request(`/approvals/${encodeURIComponent(id)}`);
      const item = result?.pending_action && typeof result.pending_action === 'object' ? result.pending_action : result;
      return item && typeof item === 'object' && item.id ? item : null;
    } catch { return null; }
  };
  const confirmTool = id => request('/tools/confirm', {method: 'POST', json: {action_id: id}});
  const targetOf = model => {
    const value = model.action.target && typeof model.action.target === 'object' ? model.action.target : model.action.arguments;
    return value && typeof value === 'object' ? value : {};
  };
  const targetNodes = value => [el('strong', {}, value.workflow_name || value.workflow_id || 'this workflow'), ' on ', el('strong', {}, value.instance_name || instanceName(value.instance_id))];
  const paintProposal = model => { model.painted = renderProposal(model.card, {...model, canOperate: can('operator'), acting: acting.has(model.action.id)}, handlersFor(model)); };
  const mountProposal = (item, anchor = null) => {
    let model = proposals.get(item.id);
    if (model) { model.action = {...model.action, ...item}; if (item.status && !acting.has(item.id)) model.status = item.status; }
    else { model = {action: item, status: item.status || 'pending', result: null, reconcile: null, feedback: null, deactivated: new Set(), forceActive: false, activated: false, card: proposalShell(item.id)}; proposals.set(item.id, model); }
    if (anchor) anchor.after(model.card); else if (!model.card.isConnected) transcript.append(model.card);
    paintProposal(model);
    return model;
  };
  const applyProposal = async model => {
    const id = model.action.id, apply = model.action.preview?.apply || {};
    if (acting.has(id)) return;
    if (apply.danger || apply.typed_confirmation) {
      const ok = await confirmAction({title: apply.label || 'Apply change', target: targetNodes(targetOf(model)), consequence: model.action.preview?.summary_text || 'This writes the change to the workflow.',
        confirmLabel: apply.label || 'Apply change', danger: Boolean(apply.danger), requireText: apply.typed_confirmation || undefined});
      if (!ok || signal.aborted) return;
    }
    acting.add(id); model.feedback = null; paintProposal(model);
    try {
      const outcome = await confirmTool(id);
      if (signal.aborted) return;
      model.status = outcome?.status || 'succeeded'; model.result = outcome?.result ?? null; model.forceActive = false;
    } catch (error) {
      if (signal.aborted) return;
      model.status = statusForError(error.code) || model.status;
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
    acting.add(id); model.feedback = null; paintProposal(model);
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
    paintProposal(model);
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
    listen(link, 'click', event => { event.preventDefault(); showInConversation(row).catch(error => { if (!signal.aborted) notice(messages, error); }); });
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
    if (!rows.length) { approvals.append(el('div',{className:'approval-empty'},icon('secrets'),el('h3',{},'No pending actions'),el('p',{},'Proposed changes appear here. Nothing runs without your confirmation.'))); return; }
    for (const row of rows) {
      const expired = Number(row.expires_at) * 1000 <= Date.now();
      const status = acting.has(row.id) ? 'executing' : row.status === 'pending' && expired ? 'expired' : row.status;
      if (isRepairAction(row)) { approvals.append(railItem(row, status)); continue; }
      const [label, tone, glyph] = APPROVAL_STATUS[status] || [String(status), 'neutral', 'unknown'];
      const card = el('article', {className: 'card approval-card'},
        el('div', {className: 'decision-head'}, el('h3', {}, ...summarize(row)), toneBadge(label, tone, glyph)),
        APPROVAL_TEXT[status] ? el('p', {}, APPROVAL_TEXT[status]) : null,
        el('p', {className: 'muted'}, `Tool ${row.name}${status === 'pending' ? ` · Expires ${formatTime(Number(row.expires_at))}` : ''}`));
      if (row.name === 'update_node' && status === 'pending') card.append(el('p', {}, 'Review the new code under Exact arguments before confirming.'));
      if (row.name === 'mcp_call') card.append(el('p', {}, 'This remote tool can change data. Its remote resource state cannot be verified.'));
      if (status === 'uncertain') card.append(el('p', {}, 'Check the remote system before requesting another action. Its outcome is unknown.'));
      card.append(rawDisclosure(row.arguments, 'Exact arguments'));
      if (status === 'pending' && can('operator')) {
        const perform = async verb => {
          if (acting.has(row.id)) return;
          acting.add(row.id);
          confirm.disabled = cancel.disabled = true;
          try {
            const result = await request(`/tools/${verb}`, {method: 'POST', json: {action_id: row.id}});
            if (!signal.aborted) { row.status = result.status; if (result.result !== undefined) messages.append(jsonView(result.result)); }
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
    mountConversationProposals();
  };

  // Chat turns. Each human action sends one request; nothing continues or retries on its own.
  const actionIds = meta => (Array.isArray(meta?.action_ids) ? meta.action_ids : Array.isArray(meta?.pending_actions) ? meta.pending_actions.map(item => item?.id) : [])
    .filter(id => typeof id === 'string' && id);
  const retryButton = turn => action('Try again', () => sendTurn(turn.continueTurn ? {continueTurn: true, message: turn.message} : {message: turn.message, retry: true}));
  const turnFooter = (meta, turnMessage) => {
    const parts = [];
    const steps = Array.isArray(meta.steps) ? meta.steps : [];
    if (steps.some(step => step?.status === 'rate_limited' || step?.code === 'provider_rate_limited')) parts.push(el('p', {className: 'repair-turn-note'}, RATE_LIMITED_TEXT));
    if (meta.code === 'provider_incomplete' || steps.some(step => step?.code === 'provider_incomplete')) parts.push(el('p', {className: 'repair-turn-note'}, INCOMPLETE_TEXT));
    if (meta.status === 'awaiting_continue') parts.push(el('p', {className: 'repair-turn-note'}, 'The AI is waiting for you. Continue sends its next request.'), action('Continue', () => sendTurn({continueTurn: true, message: turnMessage}), true));
    else if (meta.status === 'interrupted') parts.push(el('p', {className: 'repair-turn-note'}, 'This turn stopped before it finished. Nothing was applied.'), retryButton({message: turnMessage}));
    return parts.length ? el('div', {className: 'repair-turn-footer'}, parts) : null;
  };
  const appendMessage = (role, text, meta = null, turnMessage = '') => {
    transcript.querySelector('.assistant-welcome')?.remove();
    const node = el('article', {className: `chat-message chat-${role}`},el('strong', {}, role === 'user' ? 'You' : 'Inference'), text ? el('p', {className: 'pre-wrap'}, text) : null);
    if (role === 'assistant' && meta) {
      transcript.querySelectorAll('.repair-turn-footer .button').forEach(button => button.remove());
      const ids = actionIds(meta);
      if (ids.length) node.dataset.actionIds = ids.join(' ');
      if (Array.isArray(meta.steps) && meta.steps.length) node.append(turnSteps(meta.steps));
      if (Array.isArray(meta.context_log) && meta.context_log.length) node.append(contextSent(meta.context_log));
      const footer = turnFooter(meta, turnMessage);
      if (footer) node.append(footer);
    }
    transcript.append(node);
    transcript.scrollTop=transcript.scrollHeight;
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
  const sendTurn = async ({message = '', continueTurn = false, retry = false} = {}) => {
    if (busy || !provider.value || (!continueTurn && !message.trim())) return false;
    busy = true; send.disabled = true; provider.disabled = true;
    messages.replaceChildren();
    if (!continueTurn && !retry) appendMessage('user', message);
    const body = {provider_id: provider.value, ...(chatId ? {chat_id: chatId} : {}), ...(target ? {context: repairContext(target, errorTextEnabled())} : {})};
    if (continueTurn) body.continue_turn = true; else body.message = message;
    try {
      const result = await request('/chat', {method: 'POST', json: body});
      if (signal.aborted) return false;
      chatId = result?.id || result?.chat_id || chatId;
      rememberChat(chatId, message || lastMessage);
      resumeId.value = chatId || '';
      if (!continueTurn) lastMessage = message;
      if (input.value === message) input.value = '';
      const node = appendMessage('assistant', result?.content ?? '', result && typeof result === 'object' ? result : {}, message || lastMessage);
      await placeProposals(node, result);
      await refreshApprovals().catch(error => { if (!signal.aborted) notice(messages, error); });
      return true;
    } catch (error) {
      if (!signal.aborted) chatFailure(error, {message: message || lastMessage, continueTurn});
      return false;
    } finally { busy = false; if (!signal.aborted) { send.disabled = !modelsAvailable; provider.disabled = !modelsAvailable; } }
  };
  const send = el('button', {type: 'submit', className: 'button button-primary',disabled:true}, 'Send message',icon('arrow'));
  const form = el('form', {className: 'assistant-composer'}, field('Your message', input), el('div',{className:'composer-foot'},el('span',{className:'muted'},'Changes require your approval.'),send));
  listen(form, 'submit', async event => {
    event.preventDefault();
    if (busy || !input.value.trim() || !provider.value) return;
    await sendTurn({message: input.value});
  });
  listen(provider, 'change', () => { chatId = null; resumeId.value=''; showWelcome(); paintToolSupport(); });
  const newChat = action('New conversation', () => { if (busy) return; chatId = null; input.value = ''; resumeId.value = ''; showWelcome(); });
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
  const recentPicker = createIdPicker({label: 'Saved conversations', placeholder: 'Filter saved conversations',
    hint: 'Recently opened in this browser. Paste an ID below for anything else.',
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
      if (item?.role === 'user') { turnMessage = item.content; appendMessage('user', item.content); continue; }
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
  const scopeName=state.instances.find(row=>row.id===state.instanceId)?.name||'All instances';
  const chat=el('section',{className:'assistant-chat','aria-label':'Chat workspace'},
    el('div',{className:'assistant-chat-toolbar'},field('Model',provider),el('div',{className:'tool-calling'},toolBadge,toolCheck),newChat),
    repairBar,transcript,messages,form);
  const rail=el('aside',{className:'assistant-rail','aria-label':'Actions and conversation tools'},
    el('section',{className:'assistant-approval-panel'},el('div',{className:'assistant-rail-heading'},el('h2',{},'Action review'),action('Refresh proposals',refreshApprovals)),approvals),
    el('details',{className:'assistant-disclosure'},el('summary',{},'Open a saved conversation'),el('div',{className:'stack'},recentPicker.node,field('Conversation ID',resumeId),resume)),
    el('details',{className:'assistant-disclosure'},el('summary',{},'Context and privacy'),el('div',{},
      el('p',{},`Workspace selection: ${scopeName}.`),
      el('p',{},state.instanceId?`For instance-specific requests, include this ID: ${state.instanceId}`:'Include the instance ID when asking about a specific workflow or execution.'),
      el('p',{},'Messages go to the selected model provider. Built-in tools share metadata; execution inputs, outputs and stored secrets are not sent automatically.'),
      el('p',{},'With a repair target pinned, the workflow and a scrubbed error summary are also sent, with secret values masked. Open What the AI sees to review them.'))));
  container.replaceChildren(el('div',{className:'page-heading-row'},heading('AI copilot','Investigate executions. Review changes. Stay in control.'),can('admin')?action('Assistant settings',openSettings):null),el('div',{className:'assistant-layout'},chat,rail));
  transcript.append(el('p',{className:'muted',role:'status'},'Loading assistant…'));
  paintTarget();
  if (parsed.rejected.length) notice(messages, `This repair link had invalid values, which were ignored: ${parsed.rejected.join(', ')}.`);
  const context = new URLSearchParams(location.hash.split('?')[1] || '');
  const errorId = context.get('error_id');
  const repairPrompt = !target ? '' : target.error_id ? `Find the cause of error ${target.error_id} in the pinned workflow and propose a fix I can review.`
    : target.execution_id ? `Find why execution ${target.execution_id} failed and propose a fix I can review.` : 'Check the pinned workflow for problems and propose a fix I can review.';
  const explain = context.get('explain') || repairPrompt || (errorId ? `Help me investigate error ${errorId.slice(0,128)} for instance ${(context.get('instance_id') || state.instanceId).slice(0,128)}. Use available metadata and suggest next steps.` : '');
  if (explain) input.value = explain.slice(0, 16000);

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
    settings = el('details', {className: 'card assistant-settings'}, el('summary', {}, 'Providers and remote tools'));
    providerSetup(settings,refreshProviders,signal);
    const serverId = el('input', {required: true, pattern: '[A-Za-z0-9_-]{1,64}', maxLength: 64});
    const url = el('input', {type: 'url', required: true});
    const mcpKey = el('input', {placeholder: '$MCP_KEY', autocomplete: 'off'});
    const mcpSave = el('button', {type: 'submit', className: 'button button-primary'}, 'Save MCP server');
    const mcpForm = formCard({title: 'Remote MCP tools', heading: 'h3', flat: true,
      description: 'Discover a server’s tools here, then ask the assistant to propose an invocation. Every MCP call requires your confirmation.',
      body: [el('div', {className: 'form-row'}, field('Server ID', serverId, {hint: 'Letters, numbers, dashes and underscores.'}),
        field('Streamable HTTP endpoint', url, {hint: 'The server’s streamable HTTP address.'})),
        field('Key reference (optional)', mcpKey, {hint: 'A saved secret such as $MCP_KEY. Leave blank when the server needs no key.'})],
      actions: [mcpSave]});
    const catalogs = el('div', {className: 'stack'});
    const loadServers = async () => {
      const result = await request('/config/mcp');
      if (signal.aborted) return;
      catalogs.replaceChildren(...result.data.map(server => {
        const output = el('div');
        return el('div', {className: 'stack'}, el('h4', {}, server.id), el('p', {className: 'muted'}, server.url), action('Discover tools', async () => {
          const catalog = await request(`/mcp/${encodeURIComponent(server.id)}/tools`);
          if (!signal.aborted) output.replaceChildren(jsonView(catalog.data));
        }), output);
      }));
    };
    listen(mcpForm, 'submit', async event => {
      event.preventDefault(); mcpSave.disabled = true;
      try { await request('/config/mcp', {method: 'PUT', json: {id: serverId.value, config: {url: url.value, api_key_ref: mcpKey.value || null}}}); mcpKey.value = ''; await loadServers(); }
      catch (error) { if (!signal.aborted) notice(messages, error); }
      finally { mcpSave.disabled = false; }
    });
    settings.append(mcpForm, catalogs);
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
    listen(settings, 'toggle', () => { if (settings.open && !allowErrorText) renderRepairSettings(); });
    container.append(settings);
    loadServers().catch(error => { if (!signal.aborted) notice(messages, error); });
  }
  await Promise.allSettled([refreshProviders(), refreshApprovals(), loadRepairSettings()]).then(results => {
    if (!signal.aborted) results.slice(0, 2).forEach((result,index)=>{
      if(result.status !== 'rejected')return;
      notice(messages,result.reason);
      if(index===0){provider.replaceChildren(el('option',{value:''},'Models unavailable'));transcript.replaceChildren(el('div',{className:'assistant-welcome'},el('h2',{},'Could not load models'),el('p',{},'Refresh the page to try again.')));}
    });
  });
  const timer = setInterval(() => {
    if (signal.aborted) return;
    if (approvalRows.some(row => row.status === 'pending')) paintApprovals();
    // Repaint a card only when its visible status changes, so focus and open disclosures stay put.
    for (const model of proposals.values()) if (model.card.isConnected && model.painted !== effectiveStatus({...model, acting: acting.has(model.action.id)})) paintProposal(model);
  }, 15000);
  return () => { controller.abort(); clearInterval(timer); };
}
