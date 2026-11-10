"""TCP / UDP 原始套接字。按行读取文本报文（仪器 ASCII、自定义报文）。

* TCP：面向连接，连接后可按需先发一条查询指令，再读一行回复。
* UDP：面向报文，可发查询后收一条回复，也可只监听（如广播数据流）。

报文解析复用串口那套 keyvalue / csv 格式（见 ``util.parse_line``）。
"""

from __future__ import annotations

import socket

from .base import Device, DeviceError, Option, register
from .util import apply_scale, decode_text, parse_line


def _text_fields():
    """keyvalue / csv 报文共用的表单字段。"""
    return [
        Option('mode', '报文格式', 'choice', default='keyvalue', choices=['keyvalue', 'csv']),
        Option('separator', '字段分隔符', 'text', default=','),
        Option('assignment', '键值分隔符', 'text', default='='),
        Option('encoding', '编码', 'text', default='ascii'),
    ]


class _LineDevice(Device):
    """按行报文的公共部分：把一行文本解析成 ``{通道名: 数值}``。"""

    def _parse(self, raw):
        line = decode_text(raw, str(self.opt('encoding', 'ascii')))
        if not line:
            return None
        fields = parse_line(
            line,
            self.channels,
            str(self.opt('mode', 'keyvalue')),
            str(self.opt('separator', ',')),
            str(self.opt('assignment', '=')),
        )
        return apply_scale(self.channels, fields)


@register
class TcpDevice(_LineDevice):
    protocol = 'tcp'
    label = 'TCP (原始套接字)'
    channel_hint = '报文字段名'

    def __init__(self, channels=None, **options):
        super().__init__(channels, **options)
        self._sock = None
        self._reader = None

    @classmethod
    def option_fields(cls):
        return [
            Option('host', '主机 IP', 'text', default='127.0.0.1'),
            Option('port', '端口', 'int', default=9000),
            Option('timeout', '超时(秒)', 'float', default=1.0),
            Option('query', '查询指令', 'text', default='', help='每次读取前发送；留空表示只读'),
            Option('line_end', '指令结尾', 'choice', default='\\r\\n', choices=['\\r\\n', '\\n', '']),
            *_text_fields(),
        ]

    def open(self):
        host = str(self.opt('host', '127.0.0.1'))
        port = self.opt_int('port', 9000)
        timeout = self.opt_float('timeout', 1.0)
        try:
            self._sock = socket.create_connection((host, port), timeout=timeout)
            self._sock.settimeout(timeout)
            self._reader = self._sock.makefile('rb')
        except OSError as exc:
            self.close()
            raise DeviceError(f'连接 TCP {host}:{port} 失败：{exc}') from exc

    def close(self):
        for item in (self._reader, self._sock):
            try:
                if item is not None:
                    item.close()
            except OSError:
                pass
        self._reader = self._sock = None

    def read(self):
        if self._sock is None or self._reader is None:
            raise DeviceError('TCP 未连接')
        encoding = str(self.opt('encoding', 'ascii'))
        query = str(self.opt('query', ''))
        try:
            if query:
                ending = str(self.opt('line_end', '\\r\\n')).replace('\\r', '\r').replace('\\n', '\n')
                self._sock.sendall((query + ending).encode(encoding))
            raw = self._reader.readline()
        except OSError as exc:
            raise DeviceError(f'TCP 读取失败：{exc}') from exc
        return self._parse(raw)

    def describe(self):
        return f'TCP {self.opt("host", "")}:{self.opt_int("port", 9000)}'


@register
class UdpDevice(_LineDevice):
    protocol = 'udp'
    label = 'UDP (数据报)'
    channel_hint = '报文字段名'

    def __init__(self, channels=None, **options):
        super().__init__(channels, **options)
        self._sock = None
        self._addr = None

    @classmethod
    def option_fields(cls):
        return [
            Option('host', '目标 IP', 'text', default='127.0.0.1'),
            Option('port', '目标端口', 'int', default=9000),
            Option('bind_port', '本地端口', 'int', default=0, help='0 表示随机端口'),
            Option('broadcast', '启用广播', 'bool', default=False),
            Option('timeout', '超时(秒)', 'float', default=1.0),
            Option('query', '查询指令', 'text', default='', help='发送后再收一条回复；留空表示只监听'),
            *_text_fields(),
        ]

    def open(self):
        self._addr = (str(self.opt('host', '127.0.0.1')), self.opt_int('port', 9000))
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if self.opt('broadcast', False):
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self._sock.bind(('0.0.0.0', self.opt_int('bind_port', 0)))
            self._sock.settimeout(self.opt_float('timeout', 1.0))
        except OSError as exc:
            self.close()
            raise DeviceError(f'打开 UDP 失败：{exc}') from exc

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def read(self):
        if self._sock is None:
            raise DeviceError('UDP 未打开')
        query = str(self.opt('query', ''))
        try:
            if query:
                self._sock.sendto(query.encode(str(self.opt('encoding', 'ascii'))), self._addr)
            raw, _ = self._sock.recvfrom(65535)
        except TimeoutError:
            return None
        except OSError as exc:
            raise DeviceError(f'UDP 读取失败：{exc}') from exc
        return self._parse(raw)

    def describe(self):
        return f'UDP {self._addr[0]}:{self._addr[1]}' if self._addr else 'UDP'
