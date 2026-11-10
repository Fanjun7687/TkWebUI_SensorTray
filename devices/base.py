"""设备 / 协议抽象层。

新增一种协议只需在本包里加一个模块，继承 ``Device`` 并用 ``@register`` 注册，
界面会自动列出它（见 ``option_fields()``），无需改动图表代码。

最小实现::

    from .base import Channel, Device, DeviceError, Option, register

    @register
    class MyDevice(Device):
        protocol = 'mybus'
        label = 'MyBus'

        @classmethod
        def option_fields(cls):
            return [Option('host', '地址', 'text', default='127.0.0.1')]

        def open(self):
            self._link = connect(self.opt('host'))

        def close(self):
            self._link.close()

        def read(self):
            return {channel.name: read_point(channel.field) for channel in self.channels}
"""

from __future__ import annotations

from dataclasses import dataclass, field


class DeviceError(Exception):
    """设备连接或读取失败。错误信息会直接显示给用户。"""


@dataclass
class Channel:
    """一个采集通道：既描述图表曲线，也描述它在设备里的位置。"""

    name: str  # 图表显示名称，也是数据字典的键
    color: str = '#087DFF'  # 曲线颜色
    field: str = ''  # 文本寻址：报文字段名 / 主题 / JSON 路径 / OID / NodeId
    address: int = 0  # 数字寻址：Modbus 寄存器地址、CAN 帧 ID
    start: int = 0  # CAN：数据字节偏移（配合 address 表示帧 ID）
    dtype: str = 'uint16'  # 解码类型（Modbus / CAN 使用）
    function: str = ''  # Modbus：holding / input / coil；留空用设备默认
    scale: float = 1.0  # 显示值 = 原始值 * scale + offset
    offset: float = 0.0
    unit: str = ''  # 仅用于界面提示


@dataclass
class Option:
    """一个连接参数的界面描述，供自动生成配置表单。"""

    key: str
    label: str
    kind: str = 'text'  # text / int / float / choice / bool
    default: object = ''
    choices: list = field(default_factory=list)
    help: str = ''
    editable: bool = False  # choice 类型是否允许手动输入（如串口填 socket://host:port）


class Device:
    """设备基类。协议实现负责 open / close / read，图表不关心底层细节。"""

    protocol = 'device'
    label = '设备'

    # 通道「字段/地址」列的语义，供配置界面自动生成提示与解析：
    #   'field'   文本寻址：报文字段名 / 主题 / JSON 路径 / OID / NodeId
    #   'address' 数字寻址：Modbus 寄存器等
    #   'can'     CAN 帧 ID，可写 0x123 或 0x123:字节偏移
    channel_kind = 'field'
    address_radix = 10  # channel_kind == 'address' 时的进制
    dtypes = ()  # 「类型」列允许的取值；空元组表示不使用该列
    channel_hint = '字段名'  # 「字段/地址」列的界面提示

    def __init__(self, channels=None, **options):
        self.options = dict(options)
        self.channels = [
            channel if isinstance(channel, Channel) else Channel(**channel) for channel in (channels or [])
        ]

    # ---- 子类需要实现 ----------
    @classmethod
    def option_fields(cls):
        """返回连接参数的 ``Option`` 列表，供配置界面自动生成表单。"""
        return []

    def open(self):
        raise NotImplementedError

    def close(self):
        pass

    def read(self):
        """读取一批样本，返回 ``{通道名: 数值}``；暂无数据返回 ``None``。"""
        raise NotImplementedError

    # ---------- 供界面 / 图表使用 ----------
    def chart_channels(self):
        """交给 LineChart / PushDataSource 的通道定义。"""
        return [{'name': channel.name, 'color': channel.color} for channel in self.channels]

    def describe(self):
        """在数据源下拉框里显示的名字。"""
        return self.label

    # ---------- 选项读取小工具 ----------
    def opt(self, key, default=None):
        value = self.options.get(key, default)
        return default if value is None or value == '' else value

    def opt_int(self, key, default=0):
        try:
            return int(float(self.opt(key, default)))
        except (TypeError, ValueError):
            return default

    def opt_float(self, key, default=0.0):
        try:
            return float(self.opt(key, default))
        except (TypeError, ValueError):
            return default


_REGISTRY: dict[str, type[Device]] = {}


def register(cls):
    """把设备类登记进协议表（用作装饰器）。"""
    if not getattr(cls, 'protocol', None):
        raise ValueError('设备类必须定义 protocol')
    _REGISTRY[cls.protocol] = cls
    return cls


def protocols():
    """返回 ``{protocol: 设备类}``，保持注册顺序。"""
    return dict(_REGISTRY)


def create(protocol, channels=None, **options):
    """按协议名创建设备实例。"""
    cls = _REGISTRY.get(protocol)
    if cls is None:
        raise DeviceError(f'未知协议：{protocol}')
    return cls(channels=channels, **options)


def to_float(value):
    """把报文里的数字转成 float；空值或非法值返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None
