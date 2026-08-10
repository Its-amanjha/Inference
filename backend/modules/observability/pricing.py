"""Pure exact-identifier token pricing; rates are USD per million tokens."""

from decimal import Decimal, InvalidOperation, localcontext
from typing import Any


def select_prices(overrides: dict, live: dict, bundled: dict) -> dict:
    """Select whole entries; never fill an override using another source's rates."""
    return {**bundled, **live, **overrides}


def _count(value: Any) -> bool:
    return type(value) is int and value >= 0


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
    if provider == 'ollama':
        return {'cost_usd': '0', 'pricing_source': 'ollama',
                'price_timestamp': None, 'estimated': False}
    entry = prices.get(model) if model is not None else None
    if not isinstance(entry, dict) or tokens_in is None or tokens_out is None:
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
    return {'cost_usd': exact, 'pricing_source': entry.get('source'),
            'price_timestamp': entry.get('updated_at'),
            'estimated': bool((cached and cached_value is None)
                              or (reasoning and reasoning_value is None)
                              or entry.get('estimated', False))}
