"""清单记录的局部文法：从词素流里识别「清单编号 → 名称 → 单位 → 数值组」。

在真实样本 2.mfd 上核对过：14 条清单记录的编号与名称与甲方参考 XML 完全一致，
其中 2 条的单位与数值组可完整提取。**其余 12 条的单位/数值不在本文法覆盖范围内**，
按未解析记录输出（保留偏移与缺失字段），绝不补零，也绝不从参考 XML 回填。
"""
import re

CODE = re.compile(r'^\d{12}$')
MAX_UNIT_CHARS = 8
LOOKAHEAD = 40
# 数值组顺序：工程量、单价、合价。依据 2.mfd 中 010101002001 与 011707001001
# 两条记录同时与参考 XML 的 工程量/单价/合价 三个属性逐值吻合。
VALUE_FIELDS = ('工程量', '单价', '合价')


def records(tokens):
    """返回 [{offset, code, name, unit, values:{}, missing:[...]}]，顺序即文件顺序。"""
    found = []
    for position, (offset, kind, value, undecoded) in enumerate(tokens):
        if kind != 'str' or undecoded or not CODE.match(str(value)):
            continue
        name = unit = None
        numbers = []
        for _, kind2, value2, undecoded2 in tokens[position + 1:position + 1 + LOOKAHEAD]:
            if kind2 == 'field':
                continue
            if kind2 == 'str':
                if undecoded2:
                    break
                if name is None:
                    name = value2
                elif unit is None and 0 < len(str(value2)) <= MAX_UNIT_CHARS:
                    unit = value2
                else:
                    break
            elif kind2 == 'f64':
                if unit is None:
                    break  # 数值必须紧跟已识别的单位，否则无法确定它属于哪个字段
                numbers.append(value2)
                if len(numbers) >= len(VALUE_FIELDS):
                    break
        values = dict(zip(VALUE_FIELDS, numbers))
        missing = [field for field in ('名称', '单位', *VALUE_FIELDS)
                   if (field == '名称' and name is None) or (field == '单位' and unit is None)
                   or (field in VALUE_FIELDS and field not in values)]
        found.append({'offset': offset, 'code': str(value), 'name': name, 'unit': unit,
                      'values': values, 'missing': missing})
    return found
