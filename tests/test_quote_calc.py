"""Deterministic quote arithmetic: exact, reproducible, source = the inputs."""
import pytest
from factory.control.quote_calc import quote


def test_computes_exact_total_with_discount_and_rounding():
    r = quote([{'name': '产品演示', 'unit_price': '100', 'quantity': 3},
               {'name': '方案沟通', 'unit_price': '99.99', 'quantity': 2}], discount_rate='0.9')
    assert r['subtotal'] == '499.98'
    assert r['total'] == '449.98'  # 499.98 * 0.9 = 449.982 -> 449.98
    assert [l['amount'] for l in r['lines']] == ['300.00', '199.98']


def test_rejects_bad_inputs():
    for bad in ([], [{'unit_price': 'x', 'quantity': 1}], [{'unit_price': -1, 'quantity': 1}]):
        with pytest.raises(ValueError):
            quote(bad)
    with pytest.raises(ValueError):
        quote([{'unit_price': 1, 'quantity': 1}], discount_rate=0)
