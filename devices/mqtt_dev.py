"""MQTT。订阅主题并缓存最新消息（paho-mqtt，可选依赖）。

paho 在后台跑网络循环，``on_message`` 把每个主题的最新 payload 缓存下来；
``read()`` 只读缓存，因此适合事件式上报的设备（有消息才有新值）。

取值方式（``mode``）：

* ``value``：每个通道的「字段/地址」是主题，payload 本身是一个数值；
* ``keyvalue``：所有通道订阅同一个主题，payload 形如 ``T=25.6,P=101.3``，
  通道的「字段/地址」是报文字段名；
* ``json``：所有通道订阅同一个主题，payload 是 JSON，通道的「字段/地址」是 JSON 路径。
"""

from __future__ import annotations

import json
import threading

from .base import Device, DeviceError, Option, register, to_float
from .util import apply_scale, dig, parse_line

try:  # paho-mqtt 是可选依赖
    import paho.mqtt.client as mqtt

    AVAILABLE = True
except Exception:  # pragma: no cover - 依赖缺失时
    mqtt = None
    AVAILABLE = False


@register
class MqttDevice(Device):
    protocol = 'mqtt'
    label = 'MQTT (paho-mqtt)'
    channel_hint = '主题 / 字段名'

    def __init__(self, channels=None, **options):
        super().__init__(channels, **options)
        self._client = None
        self._latest = {}
        self._lock = threading.Lock()

    @classmethod
    def option_fields(cls):
        return [
            Option('host', 'Broker 地址', 'text', default='127.0.0.1'),
            Option('port', '端口', 'int', default=1883),
            Option('client_id', '客户端 ID', 'text', default='tkwebui'),
            Option('username', '用户名', 'text', default=''),
            Option('password', '密码', 'text', default=''),
            Option('tls', '使用 TLS', 'bool', default=False),
            Option('keepalive', 'Keepalive(秒)', 'int', default=30),
            Option('mode', '取值方式', 'choice', default='value', choices=['value', 'keyvalue', 'json']),
            Option('topic', '公共主题', 'text', default='', help='keyvalue / json 模式下所有通道订阅它'),
        ]

    # ---------- 连接 ----------
    def open(self):
        if not AVAILABLE:
            raise DeviceError('未安装 paho-mqtt，请执行：uv pip install paho-mqtt')
        topics = self._topics()
        if not topics:
            raise DeviceError('请至少填写一个主题（通道的「字段/地址」或「公共主题」）')
        try:
            client = self._make_client()
            client.on_message = self._on_message
            if str(self.opt('username', '')):
                client.username_pw_set(str(self.opt('username')), str(self.opt('password', '')))
            if self.opt('tls', False):
                client.tls_set()
            client.connect(
                str(self.opt('host', '127.0.0.1')), self.opt_int('port', 1883), self.opt_int('keepalive', 30)
            )
            for topic in topics:
                client.subscribe(topic, qos=1)
            client.loop_start()
            self._client = client
        except DeviceError:
            raise
        except Exception as exc:
            self.close()
            raise DeviceError(f'MQTT 连接失败：{exc}') from exc

    def _make_client(self):
        client_id = str(self.opt('client_id', 'tkwebui'))
        try:  # paho-mqtt >= 2.0 需要显式指定回调 API 版本
            return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        except (AttributeError, TypeError):
            return mqtt.Client(client_id=client_id)

    def _topics(self):
        if str(self.opt('mode', 'value')) == 'value':
            return sorted({channel.field for channel in self.channels if channel.field})
        topic = str(self.opt('topic', ''))
        return [topic] if topic else []

    def _on_message(self, client, userdata, message):
        text = message.payload.decode('utf-8', 'ignore')
        with self._lock:
            self._latest[message.topic] = text

    def close(self):
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            finally:
                self._client = None

    # ---------- 读取 ----------
    def read(self):
        with self._lock:
            snapshot = dict(self._latest)
        if not snapshot:
            return None
        mode = str(self.opt('mode', 'value'))
        if mode == 'value':
            return self._read_values(snapshot)
        payload = snapshot.get(str(self.opt('topic', '')))
        if payload is None:
            return None
        if mode == 'json':
            return self._read_json(payload)
        fields = parse_line(payload, self.channels, 'keyvalue')
        return apply_scale(self.channels, fields)

    def _read_values(self, snapshot):
        values = {}
        for channel in self.channels:
            number = to_float(snapshot.get(channel.field))
            if number is not None:
                values[channel.name] = number * channel.scale + channel.offset
        return values or None

    def _read_json(self, payload):
        try:
            data = json.loads(payload)
        except ValueError:
            return None
        values = {}
        for channel in self.channels:
            number = to_float(dig(data, channel.field))
            if number is not None:
                values[channel.name] = number * channel.scale + channel.offset
        return values or None

    def describe(self):
        return f'MQTT {self.opt("host", "")}:{self.opt_int("port", 1883)}'
