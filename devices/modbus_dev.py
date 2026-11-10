"""Modbus 协议（pymodbus）。支持 TCP 与 RTU 两种链路。

通道按功能码分组，地址相邻 / 重叠的通道会合并成一次请求，减少往返。
32 位类型支持字序切换（有些设备高字在前，有些低字在前）。
"""

from __future__ import annotations

import struct

from .base import Device, DeviceError, Option, register

try:  # pymodbus 是可选依赖
    from pymodbus.client import ModbusSerialClient, ModbusTcpClient

    AVAILABLE = True
except Exception:  # pragma: no cover - 依赖缺失时
    ModbusSerialClient = ModbusTcpClient = None
    AVAILABLE = False

BAUDRATES = ['4800', '9600', '19200', '38400', '57600', '115200']
FUNCTIONS = ['holding', 'input', 'coil']
DTYPES = ['uint16', 'int16', 'uint32', 'int32', 'float32', 'bool']
_REGISTER_COUNT = {'uint16': 1, 'int16': 1, 'uint32': 2, 'int32': 2, 'float32': 2, 'bool': 1}
_READERS = {'holding': 'read_holding_registers', 'input': 'read_input_registers', 'coil': 'read_coils'}


@register
class ModbusDevice(Device):
    protocol = 'modbus'
    label = 'Modbus (pymodbus)'
    channel_kind = 'address'
    address_radix = 10
    dtypes = DTYPES
    channel_hint = '寄存器 / 线圈地址'

    def __init__(self, channels=None, **options):
        super().__init__(channels, **options)
        self._client = None

    @classmethod
    def option_fields(cls):
        return [
            Option('mode', '连接方式', 'choice', default='tcp', choices=['tcp', 'rtu']),
            Option('host', '主机 IP', 'text', default='127.0.0.1'),
            Option('port', 'TCP 端口', 'int', default=502),
            Option(
                'serial_port', '串口 (rtu)', 'text', default='COM3', help='如 COM3；pymodbus 也支持 socket://host:port'
            ),
            Option('baudrate', '波特率 (rtu)', 'choice', default='19200', choices=BAUDRATES),
            Option('parity', '校验位 (rtu)', 'choice', default='N', choices=['N', 'E', 'O']),
            Option('unit', '从站号', 'int', default=1),
            Option('timeout', '超时(秒)', 'float', default=2.0),
            Option('function', '默认功能区', 'choice', default='holding', choices=FUNCTIONS),
            Option('word_order', '32 位字序', 'choice', default='big', choices=['big', 'little']),
        ]

    # ---------- 连接 ----------
    def open(self):
        if not AVAILABLE:
            raise DeviceError('未安装 pymodbus，请执行：uv pip install pymodbus')
        timeout = self.opt_float('timeout', 2.0)
        try:
            if str(self.opt('mode', 'tcp')) == 'tcp':
                self._client = ModbusTcpClient(
                    str(self.opt('host', '127.0.0.1')), port=self.opt_int('port', 502), timeout=timeout
                )
            else:
                self._client = self._serial_client(timeout)
            if not self._client.connect():
                raise DeviceError('无法连接到 Modbus 设备')
        except DeviceError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise DeviceError(f'连接 Modbus 设备失败：{exc}') from exc

    def _serial_client(self, timeout):
        port = str(self.opt('serial_port', 'COM3'))
        baudrate = self.opt_int('baudrate', 19200)
        parity = str(self.opt('parity', 'N'))
        try:  # pymodbus >= 3.7 使用 framer=
            from pymodbus import FramerType

            return ModbusSerialClient(
                port=port, framer=FramerType.RTU, baudrate=baudrate, parity=parity, timeout=timeout
            )
        except Exception:  # 旧版使用 method='rtu'
            return ModbusSerialClient(method='rtu', port=port, baudrate=baudrate, parity=parity, timeout=timeout)

    def close(self):
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    # ---------- 读取 ----------
    def read(self):
        if self._client is None:
            raise DeviceError('Modbus 未连接')
        unit = self.opt_int('unit', 1)
        default_function = str(self.opt('function', 'holding'))
        groups = {}
        for channel in self.channels:
            groups.setdefault(channel.function or default_function, []).append(channel)
        values = {}
        for function, channels in groups.items():
            for start, end, chunk in _merge(channels):
                response = _request(self._client, function, start, end - start, unit)
                if response is None:
                    continue
                for channel in chunk:
                    offset = channel.address - start
                    if function == 'coil':
                        bits = list(getattr(response, 'bits', []) or [])
                        if offset < len(bits):
                            values[channel.name] = (1.0 if bits[offset] else 0.0) * channel.scale + channel.offset
                    else:
                        registers = list(getattr(response, 'registers', []) or [])
                        count = _REGISTER_COUNT.get(channel.dtype, 1)
                        decoded = _decode(
                            registers[offset: offset + count], channel.dtype, str(self.opt('word_order', 'big'))
                        )
                        if decoded is not None:
                            values[channel.name] = decoded * channel.scale + channel.offset
        return values or None

    def describe(self):
        if str(self.opt('mode', 'tcp')) == 'tcp':
            return f'Modbus TCP {self.opt("host", "")}:{self.opt_int("port", 502)}'
        return f'Modbus RTU {self.opt("serial_port", "COM3")}@{self.opt("baudrate", 19200)}'


def _merge(channels):
    """把地址相邻 / 重叠的通道合并成尽量少的读请求。"""
    ranges = []
    for channel in sorted(channels, key=lambda item: item.address):
        length = _REGISTER_COUNT.get(channel.dtype, 1)
        if ranges and channel.address <= ranges[-1][1]:
            start, end, chunk = ranges[-1]
            ranges[-1] = (start, max(end, channel.address + length), chunk + [channel])
        else:
            ranges.append((channel.address, channel.address + length, [channel]))
    return ranges


def _request(client, function, address, count, unit):
    reader = getattr(client, _READERS.get(function, ''), None)
    if reader is None:
        return None
    for kwargs in ({'device_id': unit}, {'slave': unit}, {}):
        try:
            response = reader(address, count=count, **kwargs)
        except TypeError:  # 该版本不接受这个关键字，换一个
            continue
        except Exception:
            return None
        if response is None or getattr(response, 'isError', lambda: False)():
            return None
        return response
    return None


def _decode(registers, dtype, word_order='big'):
    """把寄存器（16 位字）解成数值；数据类型与字序可配置。"""
    if not registers:
        return None
    words = [int(value) & 0xFFFF for value in registers]
    try:
        if dtype == 'bool':
            return 1.0 if words[0] else 0.0
        if dtype in ('uint16', 'int16'):
            return struct.unpack('>H' if dtype == 'uint16' else '>h', words[0].to_bytes(2, 'big'))[0]
        if dtype in ('uint32', 'int32', 'float32'):
            if len(words) < 2:
                return None
            pair = words[:2] if word_order == 'big' else [words[1], words[0]]
            raw = pair[0].to_bytes(2, 'big') + pair[1].to_bytes(2, 'big')
            fmt = {'uint32': '>I', 'int32': '>i', 'float32': '>f'}[dtype]
            return struct.unpack(fmt, raw)[0]
    except (ValueError, OverflowError):
        return None
    return None
