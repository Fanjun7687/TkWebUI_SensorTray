"""设备配置对话框：协议 + 连接参数 + 采集通道。

参数表单由 ``Device.option_fields()`` 自动生成，所以新增协议时这里不用改。
返回一个尚未打开的 ``Device`` 实例。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from tkwebui import ResultDialog, Spreadsheet, ThemedCombobox, notify  # tkwebui 已通过 uv 路径依赖装进 .venv

from .base import Channel, DeviceError, create, protocols
from .util import parse_int

CHANNEL_HEADERS = ['名称', '颜色', '字段/地址', '类型', '系数', '偏移']
DEFAULT_ROWS = {
    'serial': [
        ['温度 ℃', '#087DFF', 'T', '', '1', '0'],
        ['压力 kPa', '#12B76A', 'P', '', '1', '0'],
        ['湿度 %', '#F79009', 'H', '', '1', '0'],
        ['', '', '', '', '', ''],
    ],
    'modbus': [
        ['寄存器 0', '#087DFF', '0', 'uint16', '1', '0'],
        ['流量', '#12B76A', '1', 'float32', '1', '0'],
        ['', '', '', '', '', ''],
    ],
    'tcp': [
        ['温度 ℃', '#087DFF', 'T', '', '1', '0'],
        ['压力 kPa', '#12B76A', 'P', '', '1', '0'],
        ['', '', '', '', '', ''],
    ],
    'udp': [
        ['温度 ℃', '#087DFF', 'T', '', '1', '0'],
        ['压力 kPa', '#12B76A', 'P', '', '1', '0'],
        ['', '', '', '', '', ''],
    ],
    'http': [
        ['温度 ℃', '#087DFF', 'data.temp', '', '1', '0'],
        ['压力 kPa', '#12B76A', 'data.pressure', '', '1', '0'],
        ['', '', '', '', '', ''],
    ],
    'mqtt': [
        ['温度 ℃', '#087DFF', 'sensors/temp', '', '1', '0'],
        ['压力 kPa', '#12B76A', 'sensors/pressure', '', '1', '0'],
        ['', '', '', '', '', ''],
    ],
    'opcua': [
        ['温度 ℃', '#087DFF', 'ns=2;i=2', '', '1', '0'],
        ['压力 kPa', '#12B76A', 'ns=2;i=3', '', '1', '0'],
        ['', '', '', '', '', ''],
    ],
    'can': [
        ['转速', '#087DFF', '0x123', 'uint16_be', '1', '0'],
        ['温度 ℃', '#12B76A', '0x124', 'int16_le', '0.1', '0'],
        ['', '', '', '', '', ''],
    ],
    'snmp': [
        ['CPU 负载', '#087DFF', '1.3.6.1.4.1.2021.10.1.3.1', '', '1', '0'],
        ['运行时间', '#12B76A', '1.3.6.1.2.1.1.3.0', '', '0.01', '0'],
        ['', '', '', '', '', ''],
    ],
}


def _number(text, default, label):
    if text is None or str(text).strip() == '':
        return default
    try:
        return float(text)
    except ValueError:
        raise DeviceError(f'{label}必须是数字：{text}') from None


def _trim(value):
    """把通道系数/偏移显示成简洁文本（1.0 -> '1'）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else repr(number)


class DeviceDialog(ResultDialog):
    """配置并返回一个设备实例（未连接）。"""

    def __init__(self, master, device=None):
        super().__init__(master, '连接真实设备', width=660, height=600)
        self.registry = protocols()
        if not self.registry:
            raise DeviceError('没有可用协议')
        self.values = {}  # {协议: {参数键: 值}}，切换协议时保留输入
        self._widgets = {}

        protocol = getattr(device, 'protocol', None) or next(iter(self.registry))
        self.protocol_var = tk.StringVar(self, value=protocol)
        self._build(device)

        self.primary.configure(text='连接', command=self.submit)
        self.secondary.pack(side='right', padx=8)
        ttk.Button(self.actions, text='测试读取', command=self.test_read).pack(side='left')

    # ---------- 界面 ----------
    def _build(self, device):
        self.body.columnconfigure(0, weight=1)

        box = ttk.Frame(self.body)
        box.grid(row=0, column=0, sticky='ew')
        ttk.Label(box, text='协议').pack(anchor='w')
        self.protocol_combo = ThemedCombobox(
            box, state='readonly', values=list(self.registry), textvariable=self.protocol_var
        )
        self.protocol_combo.pack(fill='x', pady=(4, 0))
        self.protocol_combo.bind('<<ComboboxSelected>>', lambda _event: self.on_protocol_change())

        self.options_card = ttk.Frame(self.body)
        self.options_card.grid(row=1, column=0, sticky='ew', pady=(14, 0))

        channels = ttk.Frame(self.body)
        channels.grid(row=2, column=0, sticky='ew', pady=(14, 0))
        channels.columnconfigure(0, weight=1)
        self.channel_label = ttk.Label(channels, text=self._channel_hint(), style='Muted.TLabel')
        self.channel_label.pack(anchor='w')
        holder = ttk.Frame(channels, height=200)
        holder.pack(fill='x', pady=(6, 0))
        holder.pack_propagate(False)
        rows = self._rows_from_device(device) or DEFAULT_ROWS.get(self.protocol_var.get(), [])
        self.sheet = Spreadsheet(
            holder,
            headers=CHANNEL_HEADERS,
            data=rows,
            rows=max(4, len(rows)),
            columns=len(CHANNEL_HEADERS),
            column_width=100,
        )
        self.sheet.pack(fill='both', expand=True)

        self.rebuild_options()

    def _channel_hint(self):
        """按当前协议说明「字段/地址」列填什么、「类型」列是否有效。"""
        cls = self.registry[self.protocol_var.get()]
        types = '可用' if cls.dtypes else '忽略'
        return f'采集通道（“字段/地址”填{cls.channel_hint}；“类型”列{types}）'

    def on_protocol_change(self):
        """切换协议时把通道表恢复成该协议的示例，避免字段名 / 寄存器地址混用。"""
        self.sheet.set_data(DEFAULT_ROWS.get(self.protocol_var.get(), []), rows=4, columns=len(CHANNEL_HEADERS))
        self.channel_label.configure(text=self._channel_hint())
        self.rebuild_options()

    def rebuild_options(self):
        """按当前协议重建参数表单。"""
        for child in self.options_card.winfo_children():
            child.destroy()
        self._widgets = {}
        protocol = self.protocol_var.get()
        fields = self.registry[protocol].option_fields()
        stored = self.values.setdefault(protocol, {})
        self.options_card.columnconfigure((0, 1), weight=1, uniform='option')
        for index, option in enumerate(fields):
            cell = ttk.Frame(self.options_card)
            cell.grid(
                row=index // 2, column=index % 2, sticky='ew', padx=(0, 14) if index % 2 == 0 else 0, pady=(0, 10)
            )
            label = option.label + ('  (提示：' + option.help + ')' if option.help else '')
            ttk.Label(cell, text=label, style='Muted.TLabel').pack(anchor='w')
            value = stored.get(option.key, option.default)
            self._widgets[option.key] = (option, self._make_input(cell, option, value))

    def _make_input(self, cell, option, value):
        if option.kind == 'choice':
            var = tk.StringVar(self, value=str(value))
            combo = ThemedCombobox(
                cell,
                values=[str(choice) for choice in option.choices],
                textvariable=var,
                state='normal' if option.editable else 'readonly',
            )
            combo.pack(fill='x', pady=(4, 0))
            return var
        if option.kind == 'bool':
            var = tk.BooleanVar(self, value=bool(value))
            ttk.Checkbutton(cell, variable=var, style='Switch.TCheckbutton').pack(anchor='w', pady=(4, 0))
            return var
        var = tk.StringVar(self, value=str(value))
        ttk.Entry(cell, textvariable=var).pack(fill='x', pady=(4, 0))
        return var

    def _rows_from_device(self, device):
        if device is None:
            return None
        kind = self.registry[device.protocol].channel_kind
        rows = []
        for channel in device.channels:
            if kind == 'can':
                target = f'0x{channel.address:X}' + (f':{channel.start}' if channel.start else '')
            elif kind == 'address':
                target = str(channel.address)
            else:
                target = channel.field or channel.name
            rows.append([channel.name, channel.color, target, channel.dtype, _trim(channel.scale), str(channel.offset)])
        return rows or None

    # ---------- 组装设备 ----------
    def build_device(self):
        protocol = self.protocol_var.get()
        stored = self.values.setdefault(protocol, {})
        options = {}
        for key, (option, var) in self._widgets.items():
            value = var.get()
            stored[key] = value
            if option.kind == 'int':
                try:
                    options[key] = int(float(value))
                except (TypeError, ValueError):
                    raise DeviceError(f'{option.label}必须是整数：{value}') from None
            elif option.kind == 'float':
                try:
                    options[key] = float(value)
                except (TypeError, ValueError):
                    raise DeviceError(f'{option.label}必须是数字：{value}') from None
            elif option.kind == 'bool':
                options[key] = bool(value)
            else:
                options[key] = str(value)
        return create(protocol, self._collect_channels(), **options)

    def _collect_channels(self):
        self.sheet.finish_edit()
        protocol = self.protocol_var.get()
        cls = self.registry[protocol]
        kind = cls.channel_kind
        allowed = tuple(cls.dtypes)
        channels = []
        for row in self.sheet.get_data():
            cells = [str(cell).strip() for cell in row]
            if not any(cells):
                continue
            name = cells[0]
            if not name:
                raise DeviceError('通道名称不能为空')
            color = cells[1] or '#087DFF'
            target = cells[2]
            scale = _number(cells[4], 1.0, f'通道「{name}」的系数')
            offset = _number(cells[5], 0.0, f'通道「{name}」的偏移')
            if kind in ('address', 'can'):
                dtype = cells[3] or (allowed[0] if allowed else '')
                if allowed and dtype not in allowed:
                    raise DeviceError(f'通道「{name}」的类型应为：{"/".join(allowed)}')
                address, start = self._parse_address(kind, cls, name, target)
                channels.append(
                    Channel(
                        name=name,
                        color=color,
                        address=address,
                        start=start,
                        dtype=dtype,
                        scale=scale,
                        offset=offset,
                    )
                )
            else:
                channels.append(Channel(name=name, color=color, field=target or name, scale=scale, offset=offset))
        if not channels:
            raise DeviceError('请至少定义一个通道')
        return channels

    @staticmethod
    def _parse_address(kind, cls, name, target):
        if kind == 'can':
            ident, _, offset_text = target.partition(':')
            try:
                return int(ident.strip() or '0', 16), int(float(offset_text or 0))
            except ValueError:
                raise DeviceError(f'通道「{name}」的 CAN 帧 ID 应为十六进制，如 0x123 或 0x123:2：{target}') from None
        try:
            return parse_int(target or 0, cls.address_radix), 0
        except ValueError:
            raise DeviceError(f'通道「{name}」的地址必须是整数：{target}') from None

    # ---------- 动作 ----------
    def submit(self):
        try:
            device = self.build_device()
        except DeviceError as exc:
            notify(self.host, str(exc), kind='danger')
            return
        self.accept(device)

    def test_read(self):
        try:
            device = self.build_device()
        except DeviceError as exc:
            notify(self.host, str(exc), kind='danger')
            return
        try:
            device.open()
            values = device.read()
        except Exception as exc:  # 硬件 / 依赖问题都反馈给用户
            notify(self.host, f'读取失败：{exc}', kind='danger')
            return
        finally:
            try:
                device.close()
            except Exception:
                pass
        if values:
            text = '，'.join(f'{key}={value:.3f}' for key, value in list(values.items())[:6])
            notify(self.host, '读取成功：' + text, kind='success')
        else:
            notify(self.host, '已连接，但本轮没有读到数据', kind='neutral')


def ask_device(master, device=None):
    """弹出对话框；用户确认时返回 ``Device``，取消返回 ``None``。"""
    return DeviceDialog(master, device=device).show_result()
