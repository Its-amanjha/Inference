"""Token usage of AI calls, read from an n8n execution. Only counts, model, provider and node names are read:
no prompt, response or other payload leaves this module.

n8n records the usage of its own AI nodes in two places (packages/workflow/src/telemetry-helpers.ts,
extractNodeTokenUsage), and this module reads them the same way:
- a language-model sub-node (type prefix ``@n8n/n8n-nodes-langchain.lm``) writes each call's usage on
  its ``ai_languageModel`` output items as ``json.tokenUsage``, or ``json.tokenUsageEstimate`` when the
  provider reported none, with ``promptTokens`` and ``completionTokens``;
- a vendor AI node (OpenAI, Anthropic, Ollama, Gemini, ...) keeps its run's usage in
  ``metadata.tokenUsage`` with ``inputTokens`` and ``outputTokens``.
The model comes from the node's parameters (``model`` or ``modelName`` on sub-nodes, ``modelId`` on vendor
nodes), as a string or a resource-locator value.

n8n counts nothing for a model called with the HTTP Request node, so the answer itself is read: the usage
block of an OpenAI (Chat Completions or Responses), Anthropic, Gemini or Ollama response, with the model the
service reports. The service comes from the request URL's host. Only HTTP Request nodes are read this way,
so a Code node passing a response along is not counted twice.
"""
from __future__ import annotations

from decimal import Decimal
import json
import re
from typing import Any
from urllib.parse import urlsplit

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
HTTP_NODE = 'n8n-nodes-base.httpRequest'
# AI services by API host, for the HTTP Request node. Any other host (or a URL built by an expression) is an
# OpenAI-compatible service of unknown price: only a price of your own, or a bundled one, applies to it.
HOSTS = {
    'api.openai.com': 'openai', 'api.anthropic.com': 'anthropic', 'generativelanguage.googleapis.com': 'google',
    'openrouter.ai': 'openrouter', 'api.groq.com': 'groq', 'api.mistral.ai': 'mistral',
    'api.deepseek.com': 'deepseek', 'api.x.ai': 'xai', 'api.together.xyz': 'together',
    'api.fireworks.ai': 'fireworks', 'api.perplexity.ai': 'perplexity'}
HOST_SUFFIXES = (('.openai.azure.com', 'azure'), ('.cognitiveservices.azure.com', 'azure'),
                 ('-aiplatform.googleapis.com', 'vertex'))
OLLAMA_PORT = 11434
MAX_NODES = 2000
MAX_RUNS = 2000
MAX_ITEMS = 1000
MAX_CALLS = 5000
TERMINAL = ('success', 'error', 'canceled', 'crashed')
_URL = re.compile(r'\s*=?\s*(https?://[^/\s?#{}]+)', re.IGNORECASE)


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


def _name(value: Any) -> str | None:
    """A model name as a service reports it."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= 256 else None


def plain(value: Decimal) -> str:
    """A decimal as plain text without trailing zeros."""
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _parameter(value: Any) -> str | None:
    """n8n's resolveParameterValue: a string, or a resource locator's string ``value``. Expressions are not names."""
    if isinstance(value, dict):
        value = value.get('value')
    name = _name(value)
    return name if name and not name.startswith('=') else None


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


def _http_provider(node: dict) -> str:
    """The AI service an HTTP Request node calls, from the literal host of its URL."""
    match = _URL.match(str(_dict(node.get('parameters')).get('url') or ''))
    try:
        parts = urlsplit(match.group(1)) if match else None
        host, port = (parts.hostname or '', parts.port) if parts else ('', None)
    except ValueError:
        host, port = '', None
    if port == OLLAMA_PORT:
        return 'ollama'
    if host in HOSTS:
        return HOSTS[host]
    for suffix, provider in HOST_SUFFIXES:
        if host.endswith(suffix):
            return provider
    return 'openai-compatible'


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


def response_usage(body: Any) -> dict | None:
    """The usage an AI service reported in one response, or None when the body is not such a response.

    ``tokens_in`` counts all input (cached reads and cache writes included) and ``tokens_out`` all output
    (reasoning included); ``cached_tokens`` and ``reasoning_tokens`` are those subsets.
    """
    body = _dict(body)
    usage, model = body.get('usage'), _name(body.get('model'))
    cached = reasoning = written = None
    if isinstance(usage, dict) and 'prompt_tokens' in usage:
        # OpenAI Chat Completions, and the services that answer in its shape (OpenRouter, Groq, Mistral, ...).
        # Embeddings report prompt tokens only.
        tokens_in, tokens_out = _count(usage.get('prompt_tokens')), _count(usage.get('completion_tokens', 0))
        cached = _count(_dict(usage.get('prompt_tokens_details')).get('cached_tokens'))
        reasoning = _count(_dict(usage.get('completion_tokens_details')).get('reasoning_tokens'))
    elif isinstance(usage, dict) and 'input_tokens' in usage and 'output_tokens' in usage:
        tokens_out = _count(usage.get('output_tokens'))
        if body.get('object') == 'response' or 'input_tokens_details' in usage:
            # OpenAI Responses: input_tokens already counts cached input.
            details = _dict(usage.get('input_tokens_details'))
            tokens_in, cached, written = _count(usage.get('input_tokens')), _count(details.get('cached_tokens')), _count(details.get('cache_write_tokens'))
            reasoning = _count(_dict(usage.get('output_tokens_details')).get('reasoning_tokens'))
        else:
            # Anthropic Messages: input_tokens leaves out cache reads and cache writes, which are input too.
            base, read = _count(usage.get('input_tokens')), _count(usage.get('cache_read_input_tokens') or 0)
            written = _count(usage.get('cache_creation_input_tokens') or 0)
            tokens_in = None if base is None or read is None or written is None else base + read + written
            cached = read or None
    elif isinstance(body.get('usageMetadata'), dict):
        # Gemini: thinking tokens are billed as output but reported apart from the answer.
        meta, model = body['usageMetadata'], _name(body.get('modelVersion')) or model
        tokens_in = _count(meta.get('promptTokenCount'))
        answer, thoughts = _count(meta.get('candidatesTokenCount') or 0), _count(meta.get('thoughtsTokenCount') or 0)
        tokens_out = None if answer is None or thoughts is None else answer + thoughts
        cached, reasoning = _count(meta.get('cachedContentTokenCount')), thoughts or None
    elif 'prompt_eval_count' in body and 'eval_count' in body:
        # Ollama's own API.
        tokens_in, tokens_out = _count(body.get('prompt_eval_count')), _count(body.get('eval_count'))
    else:
        return None
    if tokens_in is None or tokens_out is None or tokens_in > MAX_TOKENS or tokens_out > MAX_TOKENS:
        return None
    if (cached or 0) > tokens_in or (reasoning or 0) > tokens_out:
        return None
    return {'model': model, 'tokens_in': tokens_in, 'tokens_out': tokens_out, 'cached_tokens': cached or None,
            'reasoning_tokens': reasoning or None, 'cache_write_tokens': written or None}


def _http_run(task: dict, provider: str) -> list[dict]:
    """One HTTP Request run's AI calls, one entry per reported model (its items summed)."""
    calls: dict[str | None, dict] = {}
    items = 0
    for branch in _list(_dict(task.get('data')).get('main')):
        for item in _list(branch):
            items += 1
            if items > MAX_ITEMS:
                break
            body = _dict(_dict(item).get('json'))
            # With "Include Response Headers and Status" on, the answer sits in ``body``.
            if isinstance(body.get('body'), dict) and 'statusCode' in body:
                body = body['body']
            usage = response_usage(body)
            if not usage:
                continue
            call = calls.setdefault(usage['model'], {'provider': provider, 'model': usage['model'], 'tokens_in': 0, 'tokens_out': 0,
                                                     'cached_tokens': None, 'reasoning_tokens': None, 'cache_write_tokens': None})
            for key in ('tokens_in', 'tokens_out'):
                call[key] += usage[key]
            for key in ('cached_tokens', 'reasoning_tokens', 'cache_write_tokens'):
                if usage[key] is not None:
                    call[key] = (call[key] or 0) + usage[key]
    return [call for call in calls.values() if call['tokens_in'] <= MAX_TOKENS and call['tokens_out'] <= MAX_TOKENS]


def execution_calls(execution: Any) -> list[dict]:
    """Every AI call of a run that reported token counts, one entry per node run and model:
    ``{node, run, runs, provider, model, tokens_in, tokens_out, cached_tokens, reasoning_tokens,
    cache_write_tokens, estimated, source, consumers}``.

    ``run`` is the node's run index and ``runs`` how many times the node ran. ``source`` is ``n8n`` for
    n8n's own AI nodes and ``response`` for an AI service's answer to an HTTP Request node. ``consumers``
    are the nodes a language-model sub-node feeds (such as an AI Agent), so its usage can be shown on
    that step when the sub-node has no span.
    """
    execution = _dict(execution)
    workflow = _dict(execution.get('workflowData'))
    nodes = {node['name']: node for node in _list(workflow.get('nodes'))[:MAX_NODES]
             if isinstance(node, dict) and isinstance(node.get('name'), str)}
    connections = _dict(workflow.get('connections'))
    run_data = _dict(_dict(_dict(execution.get('data')).get('resultData')).get('runData'))
    calls: list[dict] = []
    for name, runs in list(run_data.items())[:MAX_NODES]:
        node = nodes.get(name)
        kind = str(_dict(node).get('type') or '')
        vendor = kind in VENDOR_NODES
        tasks = _list(runs)[:MAX_RUNS]
        if not node or not tasks:
            continue
        if vendor or kind.startswith(LM_PREFIX):
            provider, model = _provider(node), _model(node)
            consumers = [target.get('node') for branch in _list(_dict(connections.get(name)).get('ai_languageModel'))
                         for target in _list(branch) if isinstance(target, dict) and isinstance(target.get('node'), str)]
            for index, task in enumerate(tasks):
                usage = _run_usage(_dict(task), vendor)
                if usage:
                    calls.append({'node': name, 'run': index, 'runs': len(tasks), 'provider': provider, 'model': model,
                                  'tokens_in': usage['tokens_in'], 'tokens_out': usage['tokens_out'], 'cached_tokens': None,
                                  'reasoning_tokens': None, 'cache_write_tokens': None, 'estimated': usage['estimated'],
                                  'source': 'n8n', 'consumers': consumers})
        elif kind == HTTP_NODE:
            provider = _http_provider(node)
            for index, task in enumerate(tasks):
                for call in _http_run(_dict(task), provider):
                    calls.append({'node': name, 'run': index, 'runs': len(tasks), **call, 'estimated': False,
                                  'source': 'response', 'consumers': []})
        if len(calls) >= MAX_CALLS:
            return calls[:MAX_CALLS]
    return calls


def _price(call: dict, prices: dict) -> dict:
    """A call's pricing: counts reported as estimates, or cache writes (priced as ordinary input), make the
    cost an estimate."""
    pricing = calculate_cost(call['model'], call['tokens_in'], call['tokens_out'], prices, provider=call['provider'],
                             cached_tokens=call.get('cached_tokens'), reasoning_tokens=call.get('reasoning_tokens'))
    if pricing.get('cost_usd') is not None and (call.get('estimated') or call.get('cache_write_tokens')):
        pricing = {**pricing, 'estimated': True}
    return pricing


def price_calls(calls: list[dict], prices: dict) -> list[dict]:
    """Each call with ``cost_usd`` (a decimal string, or None when no price applies) and ``pricing``
    provenance. Calls already priced keep their price."""
    priced = []
    for call in calls:
        if call.get('cost_usd') is not None:
            priced.append(call)
            continue
        pricing = _price(call, prices)
        priced.append({**call, 'cost_usd': pricing.get('cost_usd'),
                       'pricing': {key: value for key, value in pricing.items() if key != 'cost_usd'}})
    return priced


def call_totals(calls: list[dict]) -> dict:
    """``known_cost`` sums the priced calls; ``cost_exact`` is the whole cost, or None while any call has no price."""
    known = sum((Decimal(call['cost_usd']) for call in calls if call.get('cost_usd') is not None), Decimal(0))
    unpriced = sum(1 for call in calls if call.get('cost_usd') is None)
    return {'ai_calls': len(calls), 'unpriced_calls': unpriced, 'known_cost': plain(known),
            'cost_exact': None if unpriced else plain(known)}


async def apply_usage(tx: Any, spans: list[dict], calls: list[dict], prices: dict) -> int:
    """Give AI calls to the spans of their steps; returns how many spans changed.

    A call goes on its node's span, or on the step a sub-node feeds when the sub-node has none (n8n may
    not emit a span for a sub-node). A node with one span per run gets each run's calls on that run's
    span; any other mismatch is left unpriced rather than guessed. Counts that telemetry reported are
    never replaced.
    """
    by_node: dict[str, list[dict]] = {}
    for span in spans:
        by_node.setdefault(span.get('node_name') or '', []).append(span)
    grouped: dict[str, list[dict]] = {}
    for call in calls:
        grouped.setdefault(call['node'], []).append(call)
    contributions: dict[int, list[dict]] = {}
    for name, entries in grouped.items():
        own = sorted(by_node.get(name) or [], key=lambda span: (span['start_ns'], span['id']))
        targets = own or sorted({span['id']: span for consumer in entries[0]['consumers'] for span in by_node.get(consumer, [])}.values(),
                                key=lambda span: (span['start_ns'], span['id']))
        if len(targets) == 1:
            pairs = [(targets[0], call) for call in entries]
        elif own and len(own) == entries[0]['runs']:
            pairs = [(own[call['run']], call) for call in entries]
        else:
            continue
        for span, call in pairs:
            contributions.setdefault(span['id'], []).append({'call': call, 'span': span})
    changed = 0
    for parts in contributions.values():
        span = parts[0]['span']
        if span.get('tokens_in') is not None or span.get('tokens_out') is not None:
            continue
        entries = [part['call'] for part in parts]
        tokens_in, tokens_out = sum(call['tokens_in'] for call in entries), sum(call['tokens_out'] for call in entries)
        priced = [_price(call, prices) for call in entries]
        cost = None
        if all(entry.get('cost_usd') is not None for entry in priced):
            cost = plain(sum((Decimal(entry['cost_usd']) for entry in priced), Decimal(0)))
        models = sorted({call['model'] for call in entries if call['model']})
        providers = sorted({call['provider'] for call in entries if call['provider']})
        sources = sorted({entry.get('pricing_source') for entry in priced if entry.get('pricing_source')})
        pricing = {'cost_usd': cost, 'pricing_source': sources[0] if len(sources) == 1 else ('several' if cost else None),
                   'price_timestamp': priced[0].get('price_timestamp') if len(priced) == 1 else None,
                   'estimated': cost is None or any(entry.get('estimated') for entry in priced),
                   'provider': providers[0] if len(providers) == 1 else None,
                   'usage_source': 'n8n execution' if all(call['source'] == 'n8n' for call in entries) else 'AI service response',
                   'usage_estimated': any(call['estimated'] for call in entries)}
        if len(priced) == 1 and priced[0].get('priced_as'):
            pricing['priced_as'] = priced[0]['priced_as']
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


async def reprice_runs(tx: Any, prices: dict, limit: int = 5000) -> int:
    """Price the AI calls of recorded n8n runs that had no price yet; returns how many calls were priced.
    Calls already priced keep their price."""
    rows = await tx.fetchall('SELECT key,calls_json FROM run_costs WHERE unpriced_calls>0 ORDER BY started_at DESC LIMIT ?', (limit,))
    changed = 0
    for row in rows:
        try:
            calls = json.loads(row['calls_json'])
        except ValueError:
            continue
        if not isinstance(calls, list) or not all(isinstance(call, dict) for call in calls):
            continue
        priced = price_calls(calls, prices)
        newly = sum(1 for before, after in zip(calls, priced) if before.get('cost_usd') is None and after.get('cost_usd') is not None)
        if not newly:
            continue
        totals = call_totals(priced)
        await tx.execute('UPDATE run_costs SET calls_json=?,unpriced_calls=?,known_cost=?,cost_exact=? WHERE key=?',
                         (json.dumps(priced), totals['unpriced_calls'], totals['known_cost'], totals['cost_exact'], row['key']))
        changed += newly
    return changed
