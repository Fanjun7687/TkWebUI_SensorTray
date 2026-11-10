"""HTTP / JSON。轮询一个 HTTP 接口，按 JSON 路径取出各通道数值。

每个通道的「字段/地址」是 JSON 路径，如 ``data.temp`` / ``list.0.value``；
也支持响应本身就是数字或 ``value`` 之类的单字段对象（配合「取值字段」选项）。
适合设备自带 REST / Web API 的场景。
"""

from __future__ import annotations

import json
import urllib.request

from .base import Device, DeviceError, Option, register, to_float
from .util import dig


@register
class HttpDevice(Device):
    protocol = 'http'
    label = 'HTTP / JSON'
    channel_hint = 'JSON 字段路径'

    @classmethod
    def option_fields(cls):
        return [
            Option('url', '接口 URL', 'text', default='http://127.0.0.1:8080/data'),
            Option('method', '请求方法', 'choice', default='GET', choices=['GET', 'POST']),
            Option('headers', '请求头', 'text', default='', help='多个用 ; 分隔，如 Authorization=Bearer abc'),
            Option('body', '请求体', 'text', default='', help='POST 时发送的 JSON 文本'),
            Option('value_key', '取值字段', 'text', default='', help='响应是 {"value": 25.6} 时填 value'),
            Option('timeout', '超时(秒)', 'float', default=2.0),
        ]

    def open(self):
        if not str(self.opt('url', '')):
            raise DeviceError('请填写接口 URL')

    def close(self):
        pass

    def _headers(self):
        headers = {'Accept': 'application/json'}
        for pair in str(self.opt('headers', '')).split(';'):
            pair = pair.strip()
            if '=' in pair:
                key, _, value = pair.partition('=')
                headers[key.strip()] = value.strip()
        return headers

    def read(self):
        url = str(self.opt('url', ''))
        method = str(self.opt('method', 'GET')).upper()
        headers = self._headers()
        body = str(self.opt('body', '')).encode('utf-8') or None
        if method == 'POST':
            headers.setdefault('Content-Type', 'application/json')
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.opt_float('timeout', 2.0)) as response:
                text = response.read().decode('utf-8', 'ignore')
        except Exception as exc:
            raise DeviceError(f'HTTP 请求失败：{exc}') from exc
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise DeviceError(f'响应不是合法 JSON：{text[:120]}') from exc

        value_key = str(self.opt('value_key', ''))
        values = {}
        for channel in self.channels:
            raw = dig(data, channel.field) if channel.field else data
            if isinstance(raw, dict) and value_key:
                raw = raw.get(value_key)
            number = to_float(raw)
            if number is not None:
                values[channel.name] = number * channel.scale + channel.offset
        return values or None

    def describe(self):
        return f'HTTP {self.opt("url", "")}'
