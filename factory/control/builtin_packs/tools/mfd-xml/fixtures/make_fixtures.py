"""生成**合成** MFD 测试样本：按已确认词素编码，业务值全部虚构。

不提交任何客户工程数据。真实样本（2.mfd）的核对结果单独记录在
docs/mfd/mfd-xml-extraction-report.md，只写结论与校验值，不进仓库。

重新生成：python3 fixtures/make_fixtures.py
"""
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def gbk(text):
    body = text.encode('gbk')
    return b'\x04\x08\x06' + bytes([len(body)]) + body


def wide(text):
    body = text.encode('utf-16le')
    return b'\x08\x12\x02' + bytes([len(body) // 2]) + body


def f64(value):
    return b'\x03\x05' + struct.pack('<d', value)


def field(tag):
    return b'\x02' + bytes([tag])


def container(count, stream):
    """^TF 头 + 0x400 处的 varint（tag 0x03，u16 记录计数）+ 记录流。"""
    head = b'^TF\x00' + b'\x00' * (0x400 - 4)
    return head + b'\x03' + struct.pack('<H', count) + stream


def record(tag, code, name, unit=None, values=()):
    out = field(tag) + gbk(code) + field(tag + 1) + wide(name)
    if unit is not None:
        out += field(tag + 2) + gbk(unit)
    for value in values:
        out += f64(value)
    return out


def main():
    complete = container(2,
        record(0x2c, '010101002001', '合成土方工程', 'm3', (12.5, 486.2, 6077.5))
        + record(0x4c, '011707001001', '合成安全文明施工', '项', (1.0, 3949.5699999999997, 3949.57)))
    (HERE / 'synthetic_complete.mfd').write_bytes(complete)

    partial = container(2,
        record(0x2c, '010101002001', '合成土方工程', 'm3', (12.5, 486.2, 6077.5))
        + record(0x61, '011707002001', '合成夜间施工'))  # 无单位、无数值：必填字段缺失
    (HERE / 'synthetic_partial.mfd').write_bytes(partial)

    (HERE / 'not_mfd.bin').write_bytes(b'PK\x03\x04' + bytes(range(256)) * 4)

    sys.path.insert(0, str(HERE.parent / 'tool'))
    from mfd_lex import lex, stream_start
    from mfd_records import records
    from xml_writer import render
    tokens, _ = lex(complete, stream_start(complete))
    (HERE / 'expected_complete.xml').write_bytes(render(records(tokens)).encode('utf-8'))
    print('已生成合成样本与期望输出')


if __name__ == '__main__':
    main()
