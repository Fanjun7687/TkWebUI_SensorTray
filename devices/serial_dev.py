"""串口协议（pyserial）。按行读取 ASCII 报文并解析成通道数值。

支持两种报文格式：

* ``keyvalue``：``T=25.6,P=101.3,F=12.0``（默认，分隔符与赋值符可配置）
* ``csv``：``25.6,101.3,12.0``（按通道定义顺序对应）

端口既可以是名字（``COM3``），也可以是 pyserial 的 URL（``socket://host:port``、
``loop://``、``rfc2217://...``），便于用网络串口服务器或自测。
"""

from __future__ import annotations

from .base import Device, DeviceError, Option, register
from .util import apply_scale, decode_text, parse_line

try:  # pyserial 是可选依赖
    import serial
    from serial.tools import list_ports

    AVAILABLE = True
except Exception:  # pragma: no cover - 依赖缺失时
    serial = None
    list_ports = None
    AVAILABLE = False

BAUDRATES = ['4800', '9600', '19200', '38400', '57600', '115200']


@register
class SerialDevice(Device):
    protocol = 'serial'
    label = '串口 (pyserial)'
    channel_hint = '报文字段名'

    def __init__(self, channels=None, **options):
        super().__init__(channels, **options)
        self._serial = None

    @classmethod
    def option_fields(cls):
        ports = [port.device for port in list_ports.comports()] if AVAILABLE else []
        return [
            Option(
                'port',
                '串口 / URL',
                'choice',
                default=ports[0] if ports else 'COM3',
                choices=ports,
                editable=True,
                help='可填 COM3，也可填 socket://host:port 之类的 pyserial URL',
            ),
            Option('baudrate', '波特率', 'choice', default='9600', choices=BAUDRATES),
            Option('bytesize', '数据位', 'choice', default='8', choices=['7', '8']),
            Option('parity', '校验位', 'choice', default='N', choices=['N', 'E', 'O']),
            Option('stopbits', '停止位', 'choice', default='1', choices=['1', '1.5', '2']),
            Option('timeout', '读取超时(秒)', 'float', default=1.0),
            Option('mode', '报文格式', 'choice', default='keyvalue', choices=['keyvalue', 'csv']),
            Option('separator', '字段分隔符', 'text', default=','),
            Option('assignment', '键值分隔符', 'text', default='='),
            Option('encoding', '编码', 'text', default='ascii'),
        ]

    # ---------- 连接 ----------
    def open(self):
        if not AVAILABLE:
            raise DeviceError('未安装 pyserial，请执行：uv pip install pyserial')
        port = str(self.opt('port', 'COM3'))
        settings = dict(
            baudrate=self.opt_int('baudrate', 9600),
            bytesize=self.opt_int('bytesize', 8),
            parity=str(self.opt('parity', 'N')),
            stopbits=self.opt_float('stopbits', 1.0),
            timeout=self.opt_float('timeout', 1.0),
        )
        try:
            if '://' in port:
                self._serial = serial.serial_for_url(port, **settings)
            else:
                self._serial = serial.Serial(port=port, **settings)
        except Exception as exc:
            raise DeviceError(f'打开串口 {port} 失败：{exc}') from exc

    def close(self):
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None

    # ---------- 读取 ----------
    def read(self):
        if self._serial is None:
            raise DeviceError('串口未打开')
        try:
            raw = self._serial.readline()
        except Exception as exc:
            raise DeviceError(f'串口读取失败：{exc}') from exc
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

    def describe(self):
        return f'串口 {self.opt("port", "?")}@{self.opt("baudrate", "?")}'
