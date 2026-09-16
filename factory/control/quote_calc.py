"""Deterministic quote arithmetic: given line items and an explicit discount rate,
return a fully broken-down, reproducible total. No model involved, so a report or
quote assistant can produce a verifiable number instead of trusting free-text math.
Monetary values use Decimal and bankers-free half-up rounding to 2 places."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation


def _money(value, label):
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValueError(f'{label} 不是有效数字：{value!r}') from None
    if d != d:  # NaN
        raise ValueError(f'{label} 不是有效数字')
    return d


def _round(d: Decimal) -> Decimal:
    return d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def quote(items, discount_rate=1) -> dict:
    """items: [{name, unit_price, quantity}]; discount_rate in (0, 1]. Returns
    per-line amounts, subtotal, the applied rate and the final total — all exact."""
    if not isinstance(items, list) or not items:
        raise ValueError('至少需要一条明细')
    if len(items) > 500:
        raise ValueError('明细过多（上限 500 条）')
    rate = _money(discount_rate, 'discount_rate')
    if not (Decimal('0') < rate <= Decimal('1')):
        raise ValueError('折扣率必须在 (0, 1] 之间')
    lines, subtotal = [], Decimal('0')
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise ValueError(f'第 {i + 1} 条明细格式无效')
        price = _money(it.get('unit_price'), f'第 {i + 1} 条单价')
        qty = _money(it.get('quantity'), f'第 {i + 1} 条数量')
        if price < 0 or qty < 0:
            raise ValueError(f'第 {i + 1} 条单价与数量不能为负')
        amount = _round(price * qty)
        subtotal += amount
        lines.append({'name': str(it.get('name', f'项目{i + 1}'))[:200],
                      'unit_price': str(price), 'quantity': str(qty), 'amount': str(amount)})
    subtotal = _round(subtotal)
    total = _round(subtotal * rate)
    return {'lines': lines, 'subtotal': str(subtotal), 'discount_rate': str(rate), 'total': str(total)}
