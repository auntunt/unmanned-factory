"""江苏未来数据标准 XML 的一个**子集**输出。

只写出真正解析到的内容。`清单` 的 单位/工程量 在 XSD 里是 required，所以缺这两项的
记录**不进 XML**——它们进 extraction_report.json，带偏移和缺失字段。宁可少写，
也不为了让文件“看起来完整”而编造必填属性。
"""
from decimal import Decimal, ROUND_HALF_UP
from xml.sax.saxutils import quoteattr

SCHEMA_NOTE = ('本文件由 MFD 局部记录提取生成，仅包含已解析的清单记录，'
               '不是无损转换；未解析记录见 extraction_report.json。')


def _decimal(value, *, money=False):
    """XSD 的 xs:decimal 不接受科学计数法，double 的二进制噪声也不该写进金额。

    金额（单价/合价）按 2 位小数 ROUND_HALF_UP 归一——造价金额以分为单位，而 MFD 里
    存的是累加后的 double（实测 3949.5699999999997）。这是**声明过的归一**，不是回填：
    原始 double 的最短往返表示同时写进 extraction_report.json，随时可核对。
    工程量不归一，保留原值。
    """
    number = Decimal(repr(float(value)))
    if money:
        number = number.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    text = format(number.normalize(), 'f')
    return text if '.' in text else text + '.0'


def render(records, *, file_type='招标文件', tax='简易计税'):
    lines = ["<?xml version='1.0' encoding='utf-8'?>",
             f'<!-- {SCHEMA_NOTE} -->',
             f'<数据转换交换标准 文件类型={quoteattr(file_type)} 计税方式={quoteattr(tax)}>',
             '  <工程信息><基本信息/></工程信息>',
             '  <单位工程 单位工程ID="DW001" 名称="" 专业类型="">',
             '    <分部分项>']
    for index, record in enumerate(records, start=1):
        attrs = {'清单ID': str(index), '附加码': '0', '编号': record['code'],
                 '名称': record['name'] or '', '项目特征': '', '单位': record['unit'] or '',
                 '工程量': _decimal(record['values']['工程量'])}
        for field in ('单价', '合价'):
            if field in record['values']:
                attrs[field] = _decimal(record['values'][field], money=True)
        rendered = ' '.join(f'{key}={quoteattr(value)}' for key, value in attrs.items())
        lines.append(f'      <清单 {rendered}/>')
    lines += ['    </分部分项>', '  </单位工程>', '</数据转换交换标准>']
    return '\n'.join(lines) + '\n'
