"""可扩展的设备 / 协议包。

导入本包会自动注册内置协议（串口、Modbus、TCP/UDP、HTTP、MQTT、OPC UA、CAN、SNMP）。
要增加新设备或新协议，在本目录新建一个模块、继承 ``Device`` 并加 ``@register``，
然后在下面 import 一次即可，图表和配置界面会自动识别。

导入顺序 = 协议在配置对话框里的排列顺序（第一个是默认协议）。
"""

from __future__ import annotations

from .base import Channel, Device, DeviceError, Option, create, protocols, register, to_float
from . import (
    serial_dev,
    modbus_dev,
    socket_dev,
    http_dev,
    mqtt_dev,
    opcua_dev,
    can_dev,
    snmp_dev,
)

__all__ = [
    'Channel',
    'Device',
    'DeviceError',
    'Option',
    'create',
    'protocols',
    'register',
    'to_float',
    'serial_dev',
    'modbus_dev',
    'socket_dev',
    'http_dev',
    'mqtt_dev',
    'opcua_dev',
    'can_dev',
    'snmp_dev',
]
