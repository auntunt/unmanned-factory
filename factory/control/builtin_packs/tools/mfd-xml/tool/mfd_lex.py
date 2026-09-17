"""MFD 容器与词法层：只实现已被证据确认的规则，其余保留偏移不猜测。

依据《MFD 格式逆向工程总结》(2026-09-04) 第 2、4 节，并在真实样本上复核：
  - 文件头签名 `^TF`；记录流起点由 0x400 处 varint 的 tag 宽度决定
    （tag 0x03 → 流始于 0x403，tag 0x04 → 0x405）。
  - `04 08 06 <u8 长度> <字节>`：单字节编码字符串。逆向笔记写的是 ASCII，
    实测该槽位承载 **GBK** 中文（如 cd da d2 bb b0 e3 = “挖一般土方”），
    因此按 GBK 解码；解不出就保留十六进制并标记 undecoded，不做替换字符糊弄。
  - `08 12 02 <u8 长度> <UTF-16LE，长度为码元数>`：宽字符串。
  - `03 05 <8 字节 IEEE754 LE>`：double。
  - `02 <tag>`：字段分隔符，tag 是记录内的字段序号（不是全局常量——
    同一语义字段在不同记录里 tag 不同，实测 02 2c / 02 4c / 02 61 都带清单编号）。

**本模块不做整体分帧。** 容器携带的是子节点个数而非字节长度，整体分帧在上游
逆向工作中仍停留在个位数百分比；这里只做局部词法扫描，因此任何“覆盖率”都
不能被解释为无损解析。
"""
import struct

SIGNATURE = b'^TF'
VARINT_WIDTH = {0x01: 0, 0x02: 1, 0x03: 2, 0x04: 4, 0x05: 8}
HEADER_VARINT_OFFSET = 0x400


class NotMfd(ValueError):
    pass


def stream_start(raw: bytes) -> int:
    """记录流起点。头部 varint 的宽度决定流从哪里开始。"""
    if raw[:3] != SIGNATURE:
        raise NotMfd('不是 MFD 文件：缺少 ^TF 签名')
    if len(raw) <= HEADER_VARINT_OFFSET + 1:
        raise NotMfd('文件过短，没有记录流')
    tag = raw[HEADER_VARINT_OFFSET]
    width = VARINT_WIDTH.get(tag)
    if width is None:
        raise NotMfd(f'流起点 varint tag 未知：0x{tag:02x}')
    return HEADER_VARINT_OFFSET + 1 + width


def lex(raw: bytes, start: int | None = None):
    """按已确认词素扫描记录流，返回 (offset, kind, value, undecoded) 元组。

    kind ∈ {'str', 'f64', 'field'}。无法识别的字节被跳过并计入 skipped——
    调用方据此说明未解释的范围，而不是假装整段都被理解了。
    """
    index = stream_start(raw) if start is None else start
    end = len(raw)
    tokens, skipped = [], 0
    while index < end:
        if raw[index:index + 3] == b'\x04\x08\x06' and index + 4 <= end:
            length = raw[index + 3]
            body = raw[index + 4:index + 4 + length]
            if len(body) == length:
                try:
                    tokens.append((index, 'str', body.decode('gbk'), False))
                except UnicodeDecodeError:
                    tokens.append((index, 'str', body.hex(), True))
                index += 4 + length
                continue
        if raw[index:index + 3] == b'\x08\x12\x02' and index + 4 <= end:
            length = raw[index + 3]
            body = raw[index + 4:index + 4 + length * 2]
            if len(body) == length * 2:
                try:
                    tokens.append((index, 'str', body.decode('utf-16le'), False))
                    index += 4 + length * 2
                    continue
                except UnicodeDecodeError:
                    pass
        if raw[index:index + 2] == b'\x03\x05' and index + 10 <= end:
            tokens.append((index, 'f64', struct.unpack('<d', raw[index + 2:index + 10])[0], False))
            index += 10
            continue
        if raw[index] == 0x02 and index + 1 < end:
            tokens.append((index, 'field', raw[index + 1], False))
            index += 2
            continue
        skipped += 1
        index += 1
    return tokens, skipped
