"""CAN / CANopen。接收 CAN 帧并按通道解码（python-can，可选依赖）。

通道的「字段/地址」填仲裁 ID（十六进制，可写 ``0x123``；需要按字节偏移取值时写成
``0x123:2``），「类型」决定如何从该帧的数据字节里解码：``uint16_be`` /
``int16_le`` / ``float32_be`` 等，字节序已含在类型名里。

采集时把总线里已有的帧全部取走，按帧 ID 只保留**最新一帧**（实时曲线只看最新值）。
"""

from __future__ import annotations

import struct

from .base import Device, DeviceError, Option, register

try:  # python-can 是可选依赖
    import can

    AVAILABLE = True
except Exception:  # pragma: no cover - 依赖缺失时
    can = None
    AVAILABLE = False

DTYPES = [
    'uint8',
    'int8',
    'uint16_be',
    'uint16_le',
    'int16_be',
    'int16_le',
    'uint32_be',
    'uint32_le',
    'int32_be',
    'int32_le',
    'float32_be',
    'float32_le',
]
_FORMATS = {
    'uint8': '>B',
    'int8': '>b',
    'uint16_be': '>H',
    'uint16_le': '<H',
    'int16_be': '>h',
    'int16_le': '<h',
    'uint32_be': '>I',
    'uint32_le': '<I',
    'int32_be': '>i',
    'int32_le': '<i',
    'float32_be': '>f',
    'float32_le': '<f',
}
INTERFACES = ['socketcan', 'pcan', 'kvaser', 'vector', 'ixxat', 'slcan', 'virtual']
BITRATES = ['125000', '250000', '500000', '1000000']


def decode_frame(data, dtype, start=0):
    """从 CAN 帧的数据字节里按 dtype 解出数值；字节不够或类型未知返回 None。"""
    fmt = _FORMATS.get(dtype)
    if fmt is None or data is None:
        return None
    size = struct.calcsize(fmt)
    raw = bytes(data)[start : start + size]
    if len(raw) < size:
        return None
    try:
        return struct.unpack(fmt, raw)[0]
    except (ValueError, struct.error):
        return None


@register
class CanDevice(Device):
    protocol = 'can'
    label = 'CAN / CANopen (python-can)'
    channel_kind = 'can'
    channel_hint = 'CAN 帧 ID（十六进制，如 0x123）'
    dtypes = DTYPES

    def __init__(self, channels=None, **options):
        super().__init__(channels, **options)
        self._bus = None
        self._latest = {}

    @classmethod
    def option_fields(cls):
        return [
            Option('interface', '接口', 'choice', default='socketcan', choices=INTERFACES),
            Option('channel', '通道', 'text', default='can0', help='如 can0 / PCAN_USBBUS1 / COM3'),
            Option('bitrate', '波特率', 'choice', default='500000', choices=BITRATES),
            Option('timeout', '接收超时(秒)', 'float', default=1.0),
        ]

    def open(self):
        if not AVAILABLE:
            raise DeviceError('未安装 python-can，请执行：uv pip install python-can')
        try:
            self._bus = can.Bus(
                interface=str(self.opt('interface', 'socketcan')),
                channel=str(self.opt('channel', 'can0')),
                bitrate=self.opt_int('bitrate', 500000),
            )
        except Exception as exc:
            self.close()
            raise DeviceError(f'打开 CAN 失败：{exc}') from exc

    def close(self):
        if self._bus is not None:
            try:
                self._bus.shutdown()
            finally:
                self._bus = None

    def read(self):
        if self._bus is None:
            raise DeviceError('CAN 未打开')
        try:
            while True:  # 取走总线上已有的帧，按 ID 保留最新
                message = self._bus.recv(timeout=0)
                if message is None:
                    break
                self._latest[message.arbitration_id] = bytes(message.data)
        except Exception as exc:
            raise DeviceError(f'CAN 读取失败：{exc}') from exc
        values = {}
        for channel in self.channels:
            decoded = decode_frame(self._latest.get(channel.address), channel.dtype, channel.start)
            if decoded is not None:
                values[channel.name] = decoded * channel.scale + channel.offset
        return values or None

    def describe(self):
        return f'CAN {self.opt("interface", "")}:{self.opt("channel", "")}'
