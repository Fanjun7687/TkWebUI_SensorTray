"""协议实现共用的小工具：文本解析、数值提取、地址解析。

串口 / TCP / UDP 等「按行报文」的协议共用这里的解析逻辑，避免重复实现。
"""

from __future__ import annotations

from .base import to_float


def decode_text(raw, encoding='ascii'):
    """把收到的 bytes 解成去掉首尾空白的文本；空数据返回空串。"""
    if raw is None:
        return ''
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).decode(encoding, 'ignore').strip()
    return str(raw).strip()


def parse_line(line, channels, mode='keyvalue', separator=',', assignment='='):
    """把一行文本按 keyvalue / csv 拆成 ``{字段名: 数值}``。"""
    if not line:
        return {}
    if mode == 'csv':
        parts = [part.strip() for part in line.split(separator)]
        return {
            (channel.field or channel.name): to_float(parts[index])
            for index, channel in enumerate(channels)
            if index < len(parts)
        }
    fields = {}
    for token in line.split(separator):
        token = token.strip()
        if not token:
            continue
        key, sep, raw = token.partition(assignment)
        if sep:
            fields[key.strip()] = to_float(raw)
    return fields


def apply_scale(channels, fields):
    """按 ``channel.scale / channel.offset`` 把 ``{字段名: 数值}`` 映射成 ``{通道名: 数值}``。"""
    values = {}
    for channel in channels:
        number = fields.get(channel.field or channel.name)
        if number is not None:
            values[channel.name] = number * channel.scale + channel.offset
    return values or None


def dig(data, path):
    """按 ``a.b.0`` 的路径从嵌套 dict / list 取值；路径为空返回原值，取不到返回 None。"""
    if not path:
        return data
    current = data
    for part in str(path).split('.'):
        if isinstance(current, dict):
            if part not in current:
                return None
            current = current[part]
        elif isinstance(current, (list, tuple)):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def parse_int(text, radix=10):
    """解析整数；radix=16 时支持 ``0x`` 前缀，radix=10 时允许 ``1.0`` 这类写法。"""
    text = str(text).strip()
    if radix == 10:
        return int(float(text or 0))
    return int(text or '0', 16)
