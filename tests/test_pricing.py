from decimal import Decimal

import pytest

from backend.modules.observability.pricing import calculate_cost, select_prices


PRICES = {'vendor/model-2026': {'input_per_million': '2', 'output_per_million': '8',
    'cached_input_per_million': '0.5', 'reasoning_output_per_million': '10',
    'source': 'override', 'updated_at': '2026-09-08'}}


def test_exact_rates_and_subsets_do_not_double_count():
    result = calculate_cost('vendor/model-2026', 1000, 500, PRICES,
                            cached_tokens=200, reasoning_tokens=100)
    assert Decimal(result['cost_usd']) == Decimal('0.0059')
    assert result['pricing_source'] == 'override'
    assert result['price_timestamp'] == '2026-09-08'
    assert result['estimated'] is False


@pytest.mark.parametrize('model', ['model', 'vendor/model', None, 'ollama/unknown'])
def test_unknown_model_never_guessed(model):
    assert calculate_cost(model, 100, 100, PRICES)['cost_usd'] is None


def test_ollama_explicit_zero_and_missing_usage_unknown():
    assert calculate_cost(None, None, None, {}, provider='ollama')['cost_usd'] == '0'
    assert calculate_cost('vendor/model-2026', None, 5, PRICES)['cost_usd'] is None


@pytest.mark.parametrize('counts', [(-1, 5, None, None), (True, 5, None, None),
    (10, 5, 11, None), (10, 5, None, 6), (10, 5, -1, None), (1.5, 5, None, None)])
def test_invalid_counts_unknown(counts):
    inp, out, cached, reasoning = counts
    assert calculate_cost('vendor/model-2026', inp, out, PRICES,
        cached_tokens=cached, reasoning_tokens=reasoning)['cost_usd'] is None


@pytest.mark.parametrize('rate', ['NaN', 'Infinity', '-1', 'nope'])
def test_invalid_rates_unknown(rate):
    assert calculate_cost('x', 1, 2, {'x': {'input_per_million': rate,
        'output_per_million': '1'}})['cost_usd'] is None


def test_missing_special_rate_marks_estimate_and_priority_is_atomic():
    bundled = {'x': {'input_per_million': '1', 'output_per_million': '2'}}
    live = {'x': {'input_per_million': '3', 'output_per_million': '4'}}
    overrides = {'x': {'input_per_million': '5', 'output_per_million': '6'}}
    prices = select_prices(overrides, live, bundled)
    assert prices['x'] == overrides['x']
    assert calculate_cost('x', 100, 100, prices, cached_tokens=50)['estimated']
    assert bundled['x']['input_per_million'] == '1'


def test_decimal_precision_is_not_lost_for_large_counts():
    value = 123456789012345678901234567890
    result = calculate_cost('x', value, 0, {'x': {
        'input_per_million': '1', 'output_per_million': '0'}})
    assert result['cost_usd'] == '123456789012345678901234.56789'


def test_null_optional_rates_are_unknown_category_rates_not_invalid_prices():
    result = calculate_cost('x', 100, 100, {'x': {
        'input_per_million': '1', 'output_per_million': '2',
        'cached_input_per_million': None, 'reasoning_output_per_million': None}},
        cached_tokens=10, reasoning_tokens=10)
    assert result['cost_usd'] == '0.0003'
    assert result['estimated'] is True
