"""OPC UA。按 NodeId 读取节点值（asyncua，可选依赖）。

使用 asyncua 的**同步客户端** ``asyncua.sync.Client``，所以可以直接在采集线程里
逐节点读取，无需自己管理事件循环。通道的「字段/地址」填 NodeId，如 ``ns=2;i=2``
或 ``ns=2;s=Temperature``。
"""

from __future__ import annotations

from .base import Device, DeviceError, Option, register, to_float

try:  # asyncua 是可选依赖
    from asyncua.sync import Client as _SyncClient

    AVAILABLE = True
except Exception:  # pragma: no cover - 依赖缺失时
    _SyncClient = None
    AVAILABLE = False


@register
class OpcUaDevice(Device):
    protocol = 'opcua'
    label = 'OPC UA (asyncua)'
    channel_hint = 'NodeId（如 ns=2;i=2）'

    def __init__(self, channels=None, **options):
        super().__init__(channels, **options)
        self._client = None

    @classmethod
    def option_fields(cls):
        return [
            Option('url', '端点 URL', 'text', default='opc.tcp://127.0.0.1:4840'),
            Option('username', '用户名', 'text', default=''),
            Option('password', '密码', 'text', default=''),
            Option('timeout', '超时(秒)', 'float', default=4.0),
        ]

    def open(self):
        if not AVAILABLE:
            raise DeviceError('未安装 asyncua，请执行：uv pip install asyncua')
        try:
            client = _SyncClient(str(self.opt('url', 'opc.tcp://127.0.0.1:4840')))
            if str(self.opt('username', '')):
                client.set_user(str(self.opt('username')))
                client.set_password(str(self.opt('password', '')))
            client.connect()
            self._client = client
        except Exception as exc:
            self.close()
            raise DeviceError(f'OPC UA 连接失败：{exc}') from exc

    def close(self):
        if self._client is not None:
            try:
                self._client.disconnect()
            finally:
                self._client = None

    def read(self):
        if self._client is None:
            raise DeviceError('OPC UA 未连接')
        values = {}
        for channel in self.channels:
            if not channel.field:
                continue
            try:
                number = to_float(self._client.get_node(channel.field).read_value())
            except Exception:  # 单个节点读失败不影响其它通道
                continue
            if number is not None:
                values[channel.name] = number * channel.scale + channel.offset
        return values or None

    def describe(self):
        return f'OPC UA {self.opt("url", "")}'
