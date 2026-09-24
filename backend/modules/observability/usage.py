"""Token usage of AI steps, read from an n8n execution the way n8n's own telemetry reads it.

n8n records usage in two places (packages/workflow/src/telemetry-helpers.ts, extractNodeTokenUsage):
- a language-model sub-node (type prefix ``@n8n/n8n-nodes-langchain.lm``) writes each call's usage on
  its ``ai_languageModel`` output items as ``json.tokenUsage``, or ``json.tokenUsageEstimate`` when the
  provider reported none, with ``promptTokens`` and ``completionTokens``;
- a vendor AI node (OpenAI, Anthropic, Ollama, Gemini, ...) keeps its run's usage in
  ``metadata.tokenUsage`` with ``inputTokens`` and ``outputTokens``.
The model comes from the node's parameters (``model`` or ``modelName`` on sub-nodes, ``modelId`` on vendor
nodes), as a string or a resource-locator value. Only counts, model names and node names are read: no
prompt, response or other payload leaves this module.
"""
from __future__ import annotations

from decimal import Decimal
import json
from typing import Any

from .pricing import MAX_TOKENS, calculate_cost

LM_PREFIX = '@n8n/n8n-nodes-langchain.lm'
VENDOR_NODES = {
    '@n8n/n8n-nodes-langchain.openAi': 'openai', '@n8n/n8n-nodes-langchain.anthropic': 'anthropic',
    '@n8n/n8n-nodes-langchain.ollama': 'ollama', '@n8n/n8n-nodes-langchain.googleGemini': 'google',
    '@n8n/n8n-nodes-langchain.alibabaCloud': 'alibaba', '@n8n/n8n-nodes-langchain.moonshot': 'moonshotai',
    '@n8n/n8n-nodes-langchain.minimax': 'minimax'}
# Language-model sub-nodes by the service they call; an unknown one is priced only by an exact override.
LM_PROVIDERS = {
    'lmChatOpenAi': 'openai', 'lmOpenAi': 'openai', 'lmChatAzureOpenAi': 'azure', 'lmChatAnthropic': 'anthropic',
    'lmChatGroq': 'groq', 'lmChatOpenRouter': 'openrouter', 'lmChatOllama': 'ollama', 'lmOllama': 'ollama',
    'lmChatGoogleGemini': 'google', 'lmChatGoogleVertex': 'vertex', 'lmChatMistralCloud': 'mistral',
    'lmChatDeepSeek': 'deepseek', 'lmChatXAiGrok': 'xai', 'lmChatAwsBedrock': 'bedrock', 'lmCohere': 'cohere',
    'lmChatVercelAiGateway': 'vercel'}
MAX_NODES = 2000
MAX_RUNS = 2000
TERMINAL = ('success', 'error', 'canceled', 'crashed')


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _count(value: Any) -> int | None:
    """A token count: a whole, bounded, nonnegative number (JSON may carry 12.0 for 12)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    number = int(value)
    return number if 0 <= number <= MAX_TOKENS else None


def _parameter(value: Any) -> str | None:
    """n8n's resolveParameterValue: a string, or a resource locator's string ``value``. Expressions are not names."""
    if isinstance(value, dict):
        value = value.get('value')
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and not value.startswith('=') and len(value) <= 256 else None


def _provider(node: dict) -> str | None:
    kind = str(node.get('type') or '')
    if kind in VENDOR_NODES:
        return VENDOR_NODES[kind]
    if not kind.startswith('@n8n/n8n-nodes-langchain.'):
        return None
    provider = LM_PROVIDERS.get(kind.rsplit('.', 1)[-1])
    # An OpenAI sub-node pointed at another base URL calls some OpenAI-compatible service, not OpenAI.
    options = _dict(_dict(node.get('parameters')).get('options'))
    base = options.get('baseURL') or options.get('baseUrl')
    if provider == 'openai' and isinstance(base, str) and base.strip() and 'api.openai.com' not in base:
        return 'openai-compatible'
    return provider


def _model(node: dict) -> str | None:
    parameters = _dict(node.get('parameters'))
    keys = ('modelId',) if node.get('type') in VENDOR_NODES else ('model', 'modelName')
    for key in keys:
        name = _parameter(parameters.get(key))
        if name:
            return name
    return None


def _run_usage(task: dict, vendor: bool) -> dict | None:
    """One run's usage, as n8n's extractNodeTokenUsage reads it."""
    tokens_in = tokens_out = 0
    found = estimated = False
    outputs = _dict(task.get('data')).get('ai_languageModel')
    if not vendor and isinstance(outputs, list):
        for branch in outputs:
            for item in _list(branch):
                json_item = _dict(_dict(item).get('json'))
                usage, guess = json_item.get('tokenUsage'), False
                if not isinstance(usage, dict):
                    usage, guess = json_item.get('tokenUsageEstimate'), True
                prompt, completion = _count(_dict(usage).get('promptTokens')), _count(_dict(usage).get('completionTokens'))
                if prompt is None or completion is None:
                    continue
                tokens_in, tokens_out, found = tokens_in + prompt, tokens_out + completion, True
                estimated = estimated or guess
    else:
        usage = _dict(_dict(task.get('metadata')).get('tokenUsage'))
        prompt, completion = _count(usage.get('inputTokens', 0)), _count(usage.get('outputTokens', 0))
        if usage and prompt is not None and completion is not None:
            tokens_in, tokens_out, found = prompt, completion, True
    if not found or tokens_in > MAX_TOKENS or tokens_out > MAX_TOKENS:
        return None
    return {'tokens_in': tokens_in, 'tokens_out': tokens_out, 'estimated': estimated}


def execution_usage(execution: Any) -> dict[str, dict]:
    """``{node name: {provider, model, runs: [{tokens_in, tokens_out, estimated}], consumers: [names]}}``.

    Only AI nodes that reported usage appear. ``consumers`` are the nodes a language-model sub-node feeds
    (such as an AI Agent), so its usage can be shown on that step when the sub-node has no span.
    """
    execution = _dict(execution)
    workflow = _dict(execution.get('workflowData'))
    nodes = {node['name']: node for node in _list(workflow.get('nodes'))[:MAX_NODES]
             if isinstance(node, dict) and isinstance(node.get('name'), str)}
    connections = _dict(workflow.get('connections'))
    run_data = _dict(_dict(_dict(execution.get('data')).get('resultData')).get('runData'))
    result: dict[str, dict] = {}
    for name, runs in list(run_data.items())[:MAX_NODES]:
        node = nodes.get(name)
        kind = str(_dict(node).get('type') or '')
        vendor = kind in VENDOR_NODES
        if not node or not (vendor or kind.startswith(LM_PREFIX)):
            continue
        usage = [entry for entry in (_run_usage(_dict(task), vendor) for task in _list(runs)[:MAX_RUNS]) if entry]
        if not usage:
            continue
        consumers = [target.get('node') for branch in _list(_dict(connections.get(name)).get('ai_languageModel'))
                     for target in _list(branch) if isinstance(target, dict) and isinstance(target.get('node'), str)]
        result[name] = {'provider': _provider(node), 'model': _model(node), 'runs': usage, 'consumers': consumers}
    return result


def _price(model: str | None, tokens_in: int, tokens_out: int, prices: dict, provider: str | None, estimated: bool) -> dict:
    pricing = calculate_cost(model, tokens_in, tokens_out, prices, provider=provider)
    if estimated and pricing.get('cost_usd') is not None:
        pricing = {**pricing, 'estimated': True}
    return pricing


async def apply_usage(tx: Any, spans: list[dict], usage: dict[str, dict], prices: dict) -> int:
    """Give usage to the spans of its steps; returns how many spans changed.

    A sub-node's usage goes on its own span, or on the step it feeds when it has none (n8n may not emit a
    span for a sub-node). Several runs go on as many spans in order; a mismatch is left unpriced rather
    than guessed. Counts that telemetry reported are never replaced.
    """
    by_node: dict[str, list[dict]] = {}
    for span in spans:
        by_node.setdefault(span.get('node_name') or '', []).append(span)
    contributions: dict[int, list[dict]] = {}
    for name, info in usage.items():
        own = sorted(by_node.get(name) or [], key=lambda span: (span['start_ns'], span['id']))
        targets = own or sorted({span['id']: span for consumer in info['consumers'] for span in by_node.get(consumer, [])}.values(),
                                key=lambda span: (span['start_ns'], span['id']))
        runs = info['runs']
        if len(targets) == 1:
            pairs = [(targets[0], {'tokens_in': sum(run['tokens_in'] for run in runs), 'tokens_out': sum(run['tokens_out'] for run in runs),
                                   'estimated': any(run['estimated'] for run in runs)})]
        elif own and len(own) == len(runs):
            pairs = list(zip(own, runs))
        else:
            continue
        for span, run in pairs:
            contributions.setdefault(span['id'], []).append({**run, 'model': info['model'], 'provider': info['provider'], 'span': span})
    changed = 0
    for parts in contributions.values():
        span = parts[0]['span']
        if span.get('tokens_in') is not None or span.get('tokens_out') is not None:
            continue
        tokens_in, tokens_out = sum(part['tokens_in'] for part in parts), sum(part['tokens_out'] for part in parts)
        priced = [_price(part['model'], part['tokens_in'], part['tokens_out'], prices, part['provider'], part['estimated']) for part in parts]
        cost = None
        if all(entry.get('cost_usd') is not None for entry in priced):
            cost = format(sum((Decimal(entry['cost_usd']) for entry in priced), Decimal(0)), 'f')
            cost = cost.rstrip('0').rstrip('.') if '.' in cost else cost
        models = sorted({part['model'] for part in parts if part['model']})
        providers = sorted({part['provider'] for part in parts if part['provider']})
        pricing = {'cost_usd': cost, 'pricing_source': priced[0].get('pricing_source') if len(priced) == 1 else ('several' if cost else None),
                   'price_timestamp': priced[0].get('price_timestamp') if len(priced) == 1 else None,
                   'estimated': cost is None or any(entry.get('estimated') for entry in priced),
                   'provider': providers[0] if len(providers) == 1 else None, 'usage_source': 'n8n execution',
                   'usage_estimated': any(part['estimated'] for part in parts)}
        model = models[0] if len(models) == 1 else (', '.join(models)[:256] if models else None)
        await tx.execute(
            'UPDATE otel_spans SET tokens_in=?,tokens_out=?,model=COALESCE(model,?),cost_exact=?,cost_usd=?,pricing_json=? '
            'WHERE id=? AND tokens_in IS NULL AND tokens_out IS NULL',
            (tokens_in, tokens_out, model, cost, float(cost) if cost is not None else None, json.dumps(pricing), span['id']))
        changed += 1
    return changed


async def reprice(tx: Any, prices: dict, where: str = '', params: tuple = (), limit: int = 5000) -> int:
    """Price AI steps that have token counts but no price yet, now that a price may exist. Prices already
    given are kept: a step costs what it cost when it was priced."""
    rows = await tx.fetchall(f'SELECT id,model,tokens_in,tokens_out,attributes_json,pricing_json FROM otel_spans '
                             f'WHERE cost_exact IS NULL AND (tokens_in IS NOT NULL OR tokens_out IS NOT NULL) {where} LIMIT ?',
                             (*params, limit))
    changed = 0
    for row in rows:
        try:
            previous = json.loads(row['pricing_json'] or '{}')
            attrs = json.loads(row['attributes_json'] or '{}')
        except ValueError:
            continue
        provider = previous.get('provider') or attrs.get('gen_ai.provider.name') or attrs.get('gen_ai.system')
        pricing = calculate_cost(row['model'], row['tokens_in'], row['tokens_out'], prices, provider=provider,
                                 cached_tokens=attrs.get('gen_ai.usage.cache_read.input_tokens'),
                                 reasoning_tokens=attrs.get('gen_ai.usage.reasoning.output_tokens'))
        if pricing.get('cost_usd') is None:
            continue
        if previous.get('usage_estimated'):
            pricing = {**pricing, 'estimated': True}
        pricing = {**{key: previous[key] for key in ('provider', 'usage_source', 'usage_estimated') if key in previous}, **pricing}
        await tx.execute('UPDATE otel_spans SET cost_exact=?,cost_usd=?,pricing_json=? WHERE id=? AND cost_exact IS NULL',
                         (pricing['cost_usd'], float(pricing['cost_usd']), json.dumps(pricing), row['id']))
        changed += 1
    return changed
