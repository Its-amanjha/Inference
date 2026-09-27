"""Pure exact-identifier token pricing; rates are USD per million tokens."""

from decimal import Decimal, InvalidOperation, localcontext
from typing import Any


def select_prices(overrides: dict, live: dict, bundled: dict) -> dict:
    """Select whole entries; never fill an override using another source's rates."""
    return {**bundled, **live, **overrides}


# More than any single model call reports; a larger count is treated as unusable, not priced.
MAX_TOKENS = 100_000_000


def _count(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_TOKENS


# OpenRouter lists models as vendor/model at the vendor's own list price. A step called through that vendor
# (or through OpenRouter) may use it; a step on another host (Groq, Azure, Bedrock, ...) pays that host's
# price, so a catalog entry never prices it: only an override or a bundled price for that exact model does.
CATALOG_VENDOR = {'openai': 'openai', 'anthropic': 'anthropic', 'google': 'google', 'mistral': 'mistralai',
                  'deepseek': 'deepseek', 'xai': 'x-ai', 'cohere': 'cohere', 'moonshotai': 'moonshotai',
                  'minimax': 'minimax', 'alibaba': 'qwen'}


# OpenAI's API answers with a dated snapshot name (gpt-4.1-2025-04-14) while the catalog lists the model
# (openai/gpt-4.1). Each snapshot here is the only one on its OpenAI model page and costs the same as the
# model (checked 2026-09-27 on developers.openai.com/api/docs/models/<model>). gpt-4o-2024-05-13 is left
# out on purpose: it was priced above gpt-4o.
SNAPSHOTS = {
    'gpt-4.1-2025-04-14': 'gpt-4.1', 'gpt-4.1-mini-2025-04-14': 'gpt-4.1-mini', 'gpt-4.1-nano-2025-04-14': 'gpt-4.1-nano',
    'gpt-4o-2024-08-06': 'gpt-4o', 'gpt-4o-2024-11-20': 'gpt-4o', 'gpt-4o-mini-2024-07-18': 'gpt-4o-mini',
    'gpt-5-2025-08-07': 'gpt-5', 'gpt-5-mini-2025-08-07': 'gpt-5-mini', 'gpt-5-nano-2025-08-07': 'gpt-5-nano',
    'o3-2025-04-16': 'o3', 'o4-mini-2025-04-16': 'o4-mini'}


def _lookup(prices: dict, model: str | None, provider: str | None) -> tuple[str | None, dict | None]:
    """The price-book key and entry that price ``model`` called through ``provider``."""
    if not isinstance(model, str) or not model:
        return None, None
    host = str(provider or '').lower()
    vendor = CATALOG_VENDOR.get(host)
    names = [model]
    # A snapshot name is translated only for OpenAI itself or OpenRouter, where it is the same model.
    bare = model.split('/', 1)[1] if model.startswith('openai/') else model
    if host in ('', 'openai', 'openrouter') and bare in SNAPSHOTS:
        names.append(('openai/' if model.startswith('openai/') else '') + SNAPSHOTS[bare])
    for name in names:
        entry = prices.get(name)
        if isinstance(entry, dict):
            from_catalog = entry.get('source') == 'openrouter'
            if not from_catalog or host in ('', 'openrouter') or (vendor and name.split('/', 1)[0] == vendor):
                return name, entry
        if vendor and '/' not in name:
            entry = prices.get(f'{vendor}/{name}')
            if isinstance(entry, dict):
                return f'{vendor}/{name}', entry
    return None, None


def find_price(prices: dict, model: str | None, provider: str | None = None) -> dict | None:
    """The price entry for a model called through ``provider``, or None. Identifiers stay exact; the only
    translations are the catalog's vendor prefix for a model named by its own vendor (gpt-4o-mini through
    OpenAI is openai/gpt-4o-mini) and the explicit ``SNAPSHOTS`` list."""
    return _lookup(prices, model, provider)[1]


def _local_ollama(provider: str | None, model: str | None) -> bool:
    """Ollama with a model that is not an Ollama Cloud one (their tags end in "cloud")."""
    return str(provider or '').lower() == 'ollama' and not str(model or '').lower().endswith('cloud')


def _rate(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError('Invalid rate')
    rate = Decimal(str(value))
    if not rate.is_finite() or rate < 0:
        raise ValueError('Invalid rate')
    return rate


def calculate_cost(
    model: str | None, tokens_in: int | None, tokens_out: int | None,
    prices: dict, provider: str | None = None, cached_tokens: int | None = None,
    reasoning_tokens: int | None = None,
) -> dict:
    """Price reported subsets once, retaining provenance and unknown usage.

    Missing special-category rates use the corresponding ordinary rate and mark
    an estimate. Explicit Ollama means zero provider charge, not zero hardware cost.
    """
    unknown = {'cost_usd': None, 'pricing_source': None,
               'price_timestamp': None, 'estimated': True}
    for value in (tokens_in, tokens_out, cached_tokens, reasoning_tokens):
        if value is not None and not _count(value):
            return unknown
    if cached_tokens is not None and (tokens_in is None or cached_tokens > tokens_in):
        return unknown
    if reasoning_tokens is not None and (tokens_out is None or reasoning_tokens > tokens_out):
        return unknown
    key, entry = _lookup(prices, model, provider)
    if not isinstance(entry, dict):
        # Last layer: a local Ollama model has no provider charge when no price applies to it.
        if _local_ollama(provider, model):
            return {'cost_usd': '0', 'pricing_source': 'ollama',
                    'price_timestamp': None, 'estimated': False}
        return unknown
    if tokens_in is None or tokens_out is None:
        return unknown
    cached, reasoning = cached_tokens or 0, reasoning_tokens or 0
    try:
        inp, out = _rate(entry['input_per_million']), _rate(entry['output_per_million'])
        cached_value = entry.get('cached_input_per_million')
        reasoning_value = entry.get('reasoning_output_per_million')
        cached_rate = _rate(cached_value) if cached and cached_value is not None else inp
        reasoning_rate = _rate(reasoning_value) if reasoning and reasoning_value is not None else out
        terms = [(tokens_in - cached, inp), (cached, cached_rate),
                 (tokens_out - reasoning, out), (reasoning, reasoning_rate)]
        # Context precision must cover integer digits and the full rate scale.
        largest = max(len(str(count)) + rate.adjusted() for count, rate in terms)
        scale = max(max(0, -rate.as_tuple().exponent) for _, rate in terms)
        with localcontext() as ctx:
            ctx.prec = max(28, largest + scale + 12)
            cost = sum((Decimal(count) * rate for count, rate in terms), Decimal(0)) / Decimal(1000000)
            exact = format(cost, 'f')
        if '.' in exact:
            exact = exact.rstrip('0').rstrip('.')
    except (InvalidOperation, ValueError, KeyError, TypeError, OverflowError):
        return unknown
    result = {'cost_usd': exact, 'pricing_source': entry.get('source'),
              'price_timestamp': entry.get('updated_at'),
              'estimated': bool((cached and cached_value is None)
                                or (reasoning and reasoning_value is None)
                                or entry.get('estimated', False))}
    if key != model:
        result['priced_as'] = key
    return result
