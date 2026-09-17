"""清单解析与 XML 生成：方法与字段映射，可独立复用。"""
import csv
import io
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from xml.sax.saxutils import escape, quoteattr

# 字段映射：CSV 表头 → XML 属性。表头缺一即拒绝，不猜列序。
COLUMNS = {'编号': 'code', '名称': 'name', '单位': 'unit', '数量': 'quantity', '单价': 'unitPrice'}
MAX_ROWS = 5000


class ParseError(ValueError):
    def __init__(self, message, code, diagnostics=None):
        super().__init__(message)
        self.code = code
        self.diagnostics = diagnostics or []


def _money(raw, field, line):
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise ParseError(f'第 {line} 行 {field} 不是数字：{raw!r}', 'bad_value') from None
    if value.is_nan() or value.is_infinite() or value < 0:
        raise ParseError(f'第 {line} 行 {field} 必须是非负有限数', 'bad_value')
    if value > Decimal('1e12'):
        raise ParseError(f'第 {line} 行 {field} 超出可处理范围', 'bad_value')
    return value


def _round(value):
    return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def parse_quote_csv(text, *, project):
    reader = csv.DictReader(io.StringIO(text))
    headers = [h.strip() for h in (reader.fieldnames or [])]
    missing = [h for h in COLUMNS if h not in headers]
    if missing:
        raise ParseError('清单缺少必需列：' + '、'.join(missing), 'bad_header')
    items, total, diagnostics, seen = [], Decimal('0'), [], set()
    for line, row in enumerate(reader, start=2):
        if line - 1 > MAX_ROWS:
            raise ParseError(f'清单超过 {MAX_ROWS} 行', 'too_many_rows')
        if all(not str(row.get(h) or '').strip() for h in COLUMNS):
            continue
        code = str(row.get('编号') or '').strip()
        name = str(row.get('名称') or '').strip()
        if not code or not name:
            raise ParseError(f'第 {line} 行缺少编号或名称', 'missing_field')
        if code in seen:
            raise ParseError(f'第 {line} 行编号重复：{code}', 'duplicate_code')
        seen.add(code)
        quantity = _money(row.get('数量'), '数量', line)
        price = _money(row.get('单价'), '单价', line)
        amount = _round(quantity * price)
        total += amount
        items.append({'code': code, 'name': name, 'unit': str(row.get('单位') or '').strip(),
                      'quantity': str(quantity), 'unitPrice': str(price), 'amount': str(amount)})
        if amount == 0:
            diagnostics.append(f'第 {line} 行合价为 0，请确认数量与单价')
    if not items:
        raise ParseError('清单没有任何有效行', 'empty')
    return {'project': project, 'items': items, 'total': str(_round(total)), 'diagnostics': diagnostics}


def render_quote_xml(quote):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<Quote project={quoteattr(quote["project"])} total={quoteattr(quote["total"])} itemCount="{len(quote["items"])}">']
    for item in quote['items']:
        attrs = ' '.join(f'{k}={quoteattr(v)}' for k, v in item.items() if k != 'name')
        lines.append(f'  <Item {attrs}>{escape(item["name"])}</Item>')
    lines.append('</Quote>')
    return '\n'.join(lines) + '\n'
