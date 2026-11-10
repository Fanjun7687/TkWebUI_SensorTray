"""SNMP（v1 / v2c）。纯标准库实现的 GET，按 OID 取值，无需额外依赖。

只实现最常用的 GET（读单个 OID）；每个通道的「字段/地址」填 OID，如
``1.3.6.1.4.1.2021.10.1.3.1``。整数 / 计数器 / 计时器 / 字符串数字都会转成数值。
"""

from __future__ import annotations

import socket

from .base import Device, DeviceError, Option, register, to_float

# ---------------------------------------------------------------- BER 编码


def _length(size):
    if size < 0x80:
        return bytes([size])
    body = b''
    while size:
        body = bytes([size & 0xFF]) + body
        size >>= 8
    return bytes([0x80 | len(body)]) + body


def _tlv(tag, body):
    return bytes([tag]) + _length(len(body)) + body


def _encode_int(value):
    if value == 0:
        return _tlv(0x02, b'\x00')
    if value > 0:
        body = value.to_bytes((value.bit_length() + 7) // 8, 'big')
        if body[0] & 0x80:
            body = b'\x00' + body
    else:
        body = value.to_bytes((value.bit_length() + 8) // 8, 'big', signed=True)
    return _tlv(0x02, body)


def _encode_oid(dotted):
    parts = [int(part) for part in str(dotted).strip('.').split('.') if part != '']
    if len(parts) < 2:
        raise DeviceError(f'非法的 OID：{dotted}')
    body = bytes([parts[0] * 40 + parts[1]])
    for value in parts[2:]:
        chunk = [value & 0x7F]
        value >>= 7
        while value:
            chunk.insert(0, (value & 0x7F) | 0x80)
            value >>= 7
        body += bytes(chunk)
    return _tlv(0x06, body)


def _read_tlv(data, index):
    tag = data[index]
    index += 1
    length = data[index]
    index += 1
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[index : index + count], 'big')
        index += count
    return tag, data[index : index + length], index + length


def _decode_value(tag, raw):
    if tag == 0x02:  # INTEGER
        return int.from_bytes(raw, 'big', signed=True)
    if tag in (0x41, 0x42, 0x43, 0x46):  # Counter32 / Gauge32 / TimeTicks / Counter64
        return int.from_bytes(raw, 'big')
    if tag == 0x04:  # OCTET STRING（有些设备把数值写成字符串）
        return to_float(raw.decode('ascii', 'ignore'))
    if tag == 0x40:  # IpAddress
        return '.'.join(str(byte) for byte in raw)
    return None


def _build_get(community, version, request_id, oid):
    """构造 GetRequest 报文；version：0 = v1，1 = v2c。"""
    varbind = _tlv(0x30, _encode_oid(oid) + _tlv(0x05, b''))
    pdu = _tlv(0xA0, _encode_int(request_id) + _encode_int(0) + _encode_int(0) + _tlv(0x30, varbind))
    return _tlv(0x30, _encode_int(version) + _tlv(0x04, community.encode('ascii', 'ignore')) + pdu)


def _parse_response(data):
    """从 GetResponse 报文里取出第一个 varbind 的值。"""
    _, message, _ = _read_tlv(data, 0)  # 外层 SEQUENCE
    index = 0
    _, _version, index = _read_tlv(message, index)
    _, _community, index = _read_tlv(message, index)
    _, pdu, index = _read_tlv(message, index)
    pdu_index = 0
    _, _request_id, pdu_index = _read_tlv(pdu, pdu_index)
    _, error_status, pdu_index = _read_tlv(pdu, pdu_index)
    _, _error_index, pdu_index = _read_tlv(pdu, pdu_index)
    _, varbinds, pdu_index = _read_tlv(pdu, pdu_index)
    if int.from_bytes(error_status, 'big', signed=True) != 0:
        return None
    _, varbind, _ = _read_tlv(varbinds, 0)
    _, _oid, varbind_index = _read_tlv(varbind, 0)
    tag, raw, _ = _read_tlv(varbind, varbind_index)
    return _decode_value(tag, raw)


# ---------------------------------------------------------------- 设备


@register
class SnmpDevice(Device):
    protocol = 'snmp'
    label = 'SNMP (v1/v2c)'
    channel_hint = 'OID'

    def __init__(self, channels=None, **options):
        super().__init__(channels, **options)
        self._request_id = 0

    @classmethod
    def option_fields(cls):
        return [
            Option('host', '主机 IP', 'text', default='192.168.1.1'),
            Option('port', '端口', 'int', default=161),
            Option('community', 'Community', 'text', default='public'),
            Option('version', '版本', 'choice', default='2c', choices=['2c', '1']),
            Option('timeout', '超时(秒)', 'float', default=2.0),
            Option('retries', '重试次数', 'int', default=1),
        ]

    def open(self):
        if not str(self.opt('host', '')):
            raise DeviceError('请填写主机 IP')

    def close(self):
        pass

    def read(self):
        values = {}
        for channel in self.channels:
            if not channel.field:
                continue
            number = to_float(self._get(channel.field))
            if number is not None:
                values[channel.name] = number * channel.scale + channel.offset
        return values or None

    def _get(self, oid):
        version = 0 if str(self.opt('version', '2c')) == '1' else 1
        self._request_id = (self._request_id + 1) & 0x7FFFFFFF
        packet = _build_get(str(self.opt('community', 'public')), version, self._request_id, oid)
        address = (str(self.opt('host', '')), self.opt_int('port', 161))
        timeout = self.opt_float('timeout', 2.0)
        for _ in range(max(1, self.opt_int('retries', 1) + 1)):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(timeout)
                try:
                    sock.sendto(packet, address)
                    data, _ = sock.recvfrom(65535)
                except TimeoutError:
                    continue
                except OSError as exc:
                    raise DeviceError(f'SNMP 请求失败：{exc}') from exc
            try:
                return _parse_response(data)
            except (IndexError, ValueError):
                return None
        return None

    def describe(self):
        return f'SNMP {self.opt("host", "")}'
