"""实时动态折线图示例（后台采集线程 + 可扩展数据源）。

运行：python examples/live_chart.py

界面用 tkinter 的 after 定时器刷新 LineChart，演示：
- 定时器驱动的实时刷新（不阻塞主线程）
- 滚动窗口与自动缩放
- 图例开关、悬停提示、点击回调、滚轮缩放与拖动平移

数据流（真实数据怎么进曲线）：

    采集线程 CaptureThread  --push()-->  PushDataSource（线程安全队列）
                                              │  poll()（主线程 after 定时器）
                                              ▼
                                          LineChart 刷新

- ``PushDataSource`` 是真实数据的入口，``push(name, value)`` 可从任意线程调用；
- ``CaptureThread`` 是一个后台采集线程示例，把 ``_read()`` 换成串口 / socket /
  文件追加 / 数据库等真实读取逻辑即可；线程**只调用 push()**，绝不触碰 Tk 控件，
  界面更新始终由主线程完成，避免 Tkinter 的线程安全问题。

真实设备（串口 / Modbus）：

    设备对话框 examples/devices/  ──>  Device.open/read/close
                                          │ read()（后台采集线程调用）
                                          ▼
                                   PushDataSource ──> LineChart

工具栏「连接设备…」可配置两种内置协议：pyserial 串口、pymodbus（TCP/RTU）。
要接入新设备或新协议，在 ``examples/devices/`` 下加一个模块、继承 ``Device`` 并加
``@register``，配置界面会自动生成对应表单，图表代码无需改动。

想用自己的数据源，实现 ``DataSource`` 的两个方法即可：

    class MySource(DataSource):
        def channels(self):
            return [{'name': '温度 ℃', 'color': '#087DFF'}]
        def poll(self):
            return read_from_device()          # 无新数据返回 None；值为 None 表示断点
    main(source=MySource())
"""

import math  # 正弦波形与 math.tau（伪数据用）
import queue  # 线程安全队列：采集线程 push() 写、主线程 poll() 读
import random  # 高斯随机游走（DriftingSignal 模拟传感器波动）
import threading  # 后台采集线程
import time  # time.monotonic() 计时，精确控制采集频率
from collections import deque  # 定长队列：滚动窗口历史缓冲（超长自动丢最旧的） Python 标准库中的双端队列类 deque，之后可以用它高效地实现队列、栈等数据结构
from datetime import datetime, timedelta  # 生成 X 轴的时间标签
import tkinter as tk  # GUI 框架
from tkinter import ttk  # 主题化控件（按钮/滑块/下拉框）

from tkwebui import enable_dpi_awareness, apply_theme, LineChart, ThemedCombobox, ValueSlider, notify  # 按需引入：DPI 适配、主题、折线图、下拉框、滑块、toast（tkwebui 已通过 uv 路径依赖装进 .venv）

# 演示通道：中文名、颜色与伪数据波形参数（base 基线，amp 振幅，period 周期，noise 噪声）
SIGNALS = [  # 三个演示通道，伪数据源与采集线程共用这份定义
    {
        'name': '温度 ℃',
        'color': '#087DFF',
        'base': 26.0,
        'amp': 4.0,
        'period': 40,
        'noise': 0.6,
    },  # 温度：基线 26，振幅 4，周期 40 步
    {
        'name': '压力 kPa',
        'color': '#19A58B',
        'base': 101.3,
        'amp': 2.5,
        'period': 65,
        'noise': 0.4,
    },  # 压力：基线 101.3，振幅 2.5，周期 65 步
    {
        'name': '流量 L/s',
        'color': '#E9A13B',
        'base': 12.0,
        'amp': 3.5,
        'period': 25,
        'noise': 1.0,
    },  # 流量：基线 12，振幅 3.5，周期 25 步
]

WINDOW_MAX = 400  # 缓冲区上限（滚动窗口最大刻度）
SEED_POINTS = 40  # 启动时预取的历史点数，界面一打开就有曲线


# =====================================================================
# 数据源接口：接入任意真实数据的入口
# =====================================================================
class DataSource:  # 数据源接口：接入任意真实数据的入口
    """数据源接口。实现 ``channels()`` 与 ``poll()`` 即可接入任意来源。

    * ``channels()`` 返回 ``[{'name': 通道名, 'color': '#RRGGBB'}, ...]``；
    * ``poll()`` 返回 ``{通道名: 数值}``，本轮没有新数据时返回 ``None``；
    * ``open()`` / ``close()`` 可选，用于连接或释放串口、文件、网络等资源。
    """

    def channels(self):  # 声明图表要画哪些通道
        raise NotImplementedError  # 子类必须实现

    def poll(self):  # 取一批新数据（主线程每个刷新周期调用一次）
        """返回 {通道名: 数值} 或 None。数值可以为 None，表示该点缺失（折线断开）。"""
        raise NotImplementedError  # 子类必须实现

    def open(self):  # 可选钩子：连接串口 / 文件 / 网络等资源
        pass

    def close(self):  # 可选钩子：释放资源
        pass


class PushDataSource(DataSource):  # 推送式数据源：真实数据接入的入口（线程安全）
    """推送式数据源：真实数据接入的入口（线程安全）。

    采集线程调用 ``push(name, value)`` 写入样本，主线程的 ``poll()`` 每个刷新周期
    把队列取出交给图表。同一通道的多个样本只保留最后一个（实时曲线只关心最新值）。
    """

    def __init__(self, channels):  # channels：通道定义列表
        self._channels = [dict(channel) for channel in channels]  # 拷贝一份，避免外部修改影响内部
        self._queue = queue.Queue()  # 线程安全队列：采集线程写、主线程读

    def channels(self):  # 返回通道定义的拷贝
        return [dict(channel) for channel in self._channels]

    def push(self, name, value):  # 写入一个样本（可从任意线程调用）
        """写入一个样本，可从任意线程调用。value 为 None 表示该点缺失。"""
        self._queue.put((name, value))  # 入队；value=None 表示该点缺失（折线断开）

    def push_many(self, mapping):  # 一次写入多个通道
        """一次写入多个通道，如 ``push_many({'温度 ℃': 25.6, '压力 kPa': 101.2})``。"""
        for name, value in mapping.items():  # 逐通道入队
            self._queue.put((name, value))

    def poll(self):  # 主线程调用：把队列里积压的样本一次取空
        batch = {}  # 聚合结果：{通道名: 最新值}
        while True:
            try:
                name, value = self._queue.get_nowait()  # 非阻塞取一个样本
            except queue.Empty:  # 队列取空了
                break
            batch[name] = value  # 同名样本只保留最后一个（实时曲线只关心最新值）
        return batch or None  # 一个都没取到就返回 None


class CallbackDataSource(DataSource):  # 拉取式数据源：每次刷新时调用 read()
    """拉取式数据源：每次刷新调用 ``read()``，适合轮询传感器 / 文件 / 数据库。"""

    def __init__(self, channels, read):  # read：返回 {通道名: 值} 的读取函数
        self._channels = [dict(channel) for channel in channels]  # 拷贝通道定义
        self._read = read  # 保存读取函数

    def channels(self):  # 返回通道定义拷贝
        return [dict(channel) for channel in self._channels]

    def poll(self):  # 直接调用读取函数并返回结果
        return self._read()


# =====================================================================
# 后台采集线程：对接 PushDataSource
# =====================================================================
class DriftingSignal:  # 随机游走并缓慢回归基线，模拟真实传感器的读数波动
    """随机游走并缓慢回归基线，模拟真实传感器的读数波动。"""

    def __init__(self, base, spread, seed):  # base 基线；spread 每步波动幅度；seed 随机种子
        self.base = float(base)  # 基线：回归目标
        self.value = float(base)  # 当前值：从基线出发
        self.spread = float(spread)  # 每步高斯噪声的标准差
        self._rng = random.Random(seed)  # 每个信号独立的随机源：互不影响且可复现

    def next(self):  # 产生下一个读数
        self.value += self._rng.gauss(0, self.spread)  # 随机走一步（正态分布）
        self.value += (self.base - self.value) * 0.02  # 缓慢回归基线，避免漂移太远
        return self.value  # 返回新读数


class CaptureThread(threading.Thread):  # 后台采集线程：读取真实数据并推进 PushDataSource
    """后台采集线程：读取真实数据并推进 PushDataSource。

    只做两件事：``reader()`` 取一批样本 → ``source.push()`` 写入。线程内不接触
    任何 Tk 控件。把默认的 ``_read()`` 换成真实读取逻辑即可，例如：

        reader=lambda: {'温度 ℃': serial_read_temp(), '压力 kPa': read_pressure()}

    退出：调用 ``stop()`` 后线程会在当前周期结束（或 0.5 秒内）安全退出。
    """

    def __init__(self, source, specs=SIGNALS, rate=20.0, reader=None):  # reader：真实读取函数；不传则用模拟读取
        super().__init__(name='tkwebui-capture', daemon=True)  # 命名并设为守护线程：主程序退出时自动结束
        self.source = source  # 数据写入的目标（PushDataSource）
        self.rate = float(rate)  # 采集频率（Hz），可在运行时修改
        self.reader = reader or self._read  # 外部传入真实读取逻辑，否则用默认模拟
        self.error = None  # 最近一次异常（供主线程读取）
        self._stop_event = threading.Event()  # 注意：不能叫 _stop，Thread 内部已占用该名字
        self._signals = {  # 为每个带 base 的通道生成一个随机游走器
            spec['name']: DriftingSignal(
                spec['base'], max(0.05, spec.get('amp', 1.0) * 0.05), index
            )  # 步长取振幅的 5%，波动不至于太剧烈
            for index, spec in enumerate(specs)  # 逐通道；index 兼作随机种子
            if 'base' in spec  # 只处理定义了基线的通道
        }

    def _read(self):  # 默认的模拟读取（换成真实采集代码即可）
        """默认模拟读取：把这里换成你的真实采集代码（串口 / socket / 文件 / 数据库）。"""
        return {name: signal.next() for name, signal in self._signals.items()}  # 每通道产生一个新读数

    def run(self):  # 线程主体：循环采集直到 stop()
        while not self._stop_event.is_set():  # 未收到停止信号就继续
            started = time.monotonic()  # 本轮开始时间（用于精确控频）
            try:
                batch = self.reader()  # 读一批样本（真实读取或模拟）
            except Exception as exc:  # 采集出错不崩溃，记录后重试
                self.error = f'{type(exc).__name__}: {exc}'  # 记录异常，供主线程提示
                self._stop_event.wait(0.5)  # 等 0.5 秒再试，避免刷屏
                continue  # 跳过本轮推送
            if batch:  # 读到数据才推送
                for name, value in batch.items():  # 逐通道写入
                    self.source.push(name, value)  # 线程内绝不触碰 Tk，只 push()
            delay = 1.0 / max(0.1, self.rate) - (time.monotonic() - started)  # 目标周期 − 已耗时间
            self._stop_event.wait(max(0.0, delay))  # 等剩余时间（同时能及时响应停止）

    def stop(self):  # 请求停止：线程在当前周期结束（或 0.5 秒内）退出
        self._stop_event.set()


class FakeSensor:  # 用正弦叠加缓慢漂移，生成平滑的伪数据（预填历史用）
    """用正弦叠加缓慢漂移，生成平滑的伪数据（预填历史用）。"""

    def __init__(self, spec):  # spec：SIGNALS 里的一条通道定义
        self.name = spec['name']  # 通道名
        self.color = spec['color']  # 曲线颜色
        self.base = spec['base']  # 基线
        self.amp = spec['amp']  # 振幅
        self.period = spec['period']  # 主波周期（采样步数）
        self.noise = spec['noise']  # 抖动幅度
        self.phase = (hash(self.name) % 100) / 100 * math.tau  # 由通道名决定初相位：三条曲线错开

    def value(self, tick):  # 计算第 tick 步的伪读数
        wave = math.sin(tick / self.period * math.tau + self.phase)  # 主正弦波
        drift = 0.35 * math.sin(tick / (self.period * 3.7) * math.tau)  # 慢漂移：周期是主波的 3.7 倍
        jitter = math.sin(tick * 12.9898) * 43758.5453  # 可复现的伪随机抖动
        jitter = (jitter - math.floor(jitter) - 0.5) * 2  # 取小数部分并映射到 [-1, 1]
        return self.base + self.amp * (0.6 * wave + 0.4 * drift) + self.noise * jitter  # 叠加成最终读数


class FakeDataSource(DataSource):  # 内置伪数据源：演示和预填历史用
    """内置伪数据源，用于演示和预填历史。"""

    def __init__(self, specs=SIGNALS):  # specs：通道定义列表
        self._sensors = [FakeSensor(spec) for spec in specs]  # 每个通道一个伪传感器
        self._tick = 0  # 采样序号（时间轴）

    def channels(self):  # 通道名与颜色
        return [{'name': sensor.name, 'color': sensor.color} for sensor in self._sensors]

    def poll(self):  # 序号推进一格，返回所有传感器当前值
        self._tick += 1
        return {sensor.name: sensor.value(self._tick) for sensor in self._sensors}


# =====================================================================
# 界面
# =====================================================================
class LiveChartApp(tk.Tk):
    def __init__(self, source=None, device=None):  # source：自定义数据源；device：启动即连接的真实设备
        super().__init__()  # 初始化 Tk 根窗口
        self.title('TkWebUI · 实时动态折线图（后台采集线程）')  # 窗口标题
        self.geometry('980x720')  # 初始尺寸
        self.minsize(780, 560)  # 最小尺寸
        apply_theme(self)  # 必须在创建控件之前调用

        # 注册可选的数据源：内置伪数据、PushDataSource，以及可选的 source 参数。
        self.builtin_source = FakeDataSource()  # 内置伪数据源
        self.push_source = PushDataSource(SIGNALS)  # 外部推送源：真实数据的入口
        self.source_registry = [
            ('内置伪数据', self.builtin_source),
            ('外部推送（采集线程）', self.push_source),
        ]  # 下拉框选项：(显示名, 数据源)
        if source is not None:  # 外部传入的自定义数据源排在最前
            self.source_registry.insert(0, ('自定义数据源', source))

        self.source = source if source is not None else self.builtin_source  # 当前使用的数据源
        self.current_label = self.source_registry[0][0] if source is not None else '内置伪数据'  # 当前源的显示名

        self.channels = []  # 当前通道列表 [{'name', 'color'}, ...]
        self.history = {}  # 每通道的滚动历史缓冲 {通道名: deque}
        self.stamps = deque(maxlen=WINDOW_MAX)  # X 轴时间标签缓冲
        self.tick = 0  # 已产生的采样序号
        self.job = None  # after 定时器句柄
        self.capture = None  # 当前采集线程
        self.device = None  # 当前已连接的真实设备（有采集线程时非空）
        self.running = False  # 主循环是否在刷新
        self._load_channels(self.source.channels())  # 按数据源的通道定义建好缓冲区

        self.auto_scale = tk.BooleanVar(self, value=True)  # 每帧自动适应范围
        self.interval = tk.DoubleVar(self, value=300)  # 刷新间隔（毫秒）
        self.window = tk.DoubleVar(self, value=60)  # 显示窗口点数
        self.capture_rate = tk.DoubleVar(self, value=20)  # 采集频率（Hz）
        self.source_var = tk.StringVar(self, value=self.current_label)  # 数据源下拉框当前值
        self.status = tk.StringVar(self, value='就绪')  # 状态栏文字

        self.build()  # 创建全部控件
        self.seed(SEED_POINTS)  # 先铺一段历史数据
        self.refresh()  # 首次绘制
        self.protocol('WM_DELETE_WINDOW', self.close)  # 点窗口 × 执行 close()（托盘版会改成“隐藏”）
        if device is not None:  # 命令行 / 代码里直接接一台真实设备
            self.after(0, lambda: self.connect_device(device))  # 主循环起来后自动连接
        self.after(300, self.start)  # 打开窗口后自动开始刷新

    # ---------- 界面 ----------
    def build(self):  # 创建工具栏、参数区、底部状态区与图表
        toolbar = ttk.Frame(self, padding=(16, 12))  # 顶部工具栏
        toolbar.pack(fill='x')
        self.toggle_button = ttk.Button(
            toolbar, text='暂停', width=8, style='Primary.TButton', command=self.toggle
        )  # 暂停/开始刷新按钮
        self.toggle_button.pack(side='left')
        self.capture_button = ttk.Button(
            toolbar, text='启动采集', width=10, command=self.toggle_capture
        )  # 启动/停止后台采集线程
        self.capture_button.pack(side='left', padx=(8, 0))
        self.device_button = ttk.Button(
            toolbar, text='连接设备…', width=11, command=self.open_device_dialog
        )  # 弹出设备配置对话框
        self.device_button.pack(side='left', padx=(8, 0))
        ttk.Button(toolbar, text='清空', style='Quiet.TButton', command=self.clear).pack(
            side='left', padx=(8, 0)
        )  # 清空所有缓冲数据
        ttk.Button(toolbar, text='重置视图', style='Quiet.TButton', command=self.reset_view).pack(  # 恢复自动缩放与视图
            side='left', padx=(8, 0)
        )
        ttk.Checkbutton(
            toolbar, text='自动缩放', style='Switch.TCheckbutton', variable=self.auto_scale
        ).pack(  # 每帧是否重置坐标范围
            side='right'
        )
        ttk.Label(toolbar, text='滚轮缩放 · 拖动平移 · 点击图例隐藏系列', style='Muted.TLabel').pack(  # 操作提示文字
            side='left', padx=(16, 0)
        )

        controls = ttk.Frame(self, padding=(16, 0))  # 参数设置区
        controls.pack(fill='x')
        controls.columnconfigure((0, 1), weight=1, uniform='ctrl')  # 两列等宽

        def field(row, column, text):  # 小工具函数：建一个带标题的输入格子
            box = ttk.Frame(controls)  # 格子容器
            box.grid(  # 网格布局：第一列右侧留 16px，第一行下方留 10px
                row=row, column=column, sticky='ew', padx=(0, 16) if column == 0 else 0, pady=(0, 10) if row == 0 else 0
            )
            ttk.Label(box, text=text).pack(anchor='w')  # 格子标题
            return box

        box = field(0, 0, '数据源')  # 第 0 行第 0 列：数据源选择
        self.source_combo = ThemedCombobox(  # 下拉框：只读，选项来自数据源注册表
            box, state='readonly', values=[label for label, _ in self.source_registry], textvariable=self.source_var
        )
        self.source_combo.pack(fill='x', pady=(4, 0))
        self.source_combo.bind('<<ComboboxSelected>>', self.on_source_change)  # 选中即切换数据源

        box = field(0, 1, '刷新间隔（主线程重绘）')  # 第 0 行第 1 列：刷新间隔
        ValueSlider(box, variable=self.interval, from_=100, to=2000, step=100, unit='ms').pack(
            fill='x', pady=(4, 0)
        )  # 100–2000ms

        box = field(1, 0, '采集频率（后台线程）')  # 第 1 行第 0 列：采集频率
        ValueSlider(
            box,
            variable=self.capture_rate,
            from_=1,
            to=100,
            step=1,
            unit='Hz',
            command=self.on_rate_change,  # 拖动即改线程频率
        ).pack(fill='x', pady=(4, 0))

        box = field(1, 1, '显示窗口点数')  # 第 1 行第 1 列：窗口长度
        ValueSlider(
            box,
            variable=self.window,
            from_=20,
            to=200,
            step=10,
            unit='点',
            command=lambda value: self.refresh(),  # 拖动立即重绘
        ).pack(fill='x', pady=(4, 0))

        footer = ttk.Frame(self, padding=(16, 8))  # 底部区域
        footer.pack(side='bottom', fill='x')
        self.readout = ttk.Frame(footer)  # 左侧：各通道最新读数
        self.readout.pack(side='left')
        ttk.Label(footer, textvariable=self.status, style='Muted.TLabel').pack(side='right')  # 右侧：状态栏
        self.rebuild_readout()  # 按当前通道生成读数标签

        body = ttk.Frame(self, padding=(16, 12))  # 图表容器
        body.pack(fill='both', expand=True)
        self.chart = LineChart(  # 折线图：初始系列、悬浮提示格式器、点击回调
            body, series=self.series(), labels=[], height=320, tooltip_formatter=self.tooltip, on_click=self.on_point
        )
        self.chart.pack(fill='both', expand=True)  # 填满剩余空间

    def rebuild_readout(self):  # 按当前通道重建底部读数标签（切换数据源时调用）
        """按当前通道重建底部读数标签（切换数据源时调用）。"""
        for label in getattr(self, 'readout_labels', []):  # 先销毁旧标签
            label.destroy()
        self.readout_labels = []  # 重建标签列表
        for channel in self.channels:  # 每个通道一个彩色标签
            label = ttk.Label(self.readout, text=f"● {channel['name']}  --", foreground=channel['color'])  # 初始显示 --
            label.pack(side='left', padx=(0, 20))
            self.readout_labels.append(label)

    # ---------- 数据源管理 ----------
    def _load_channels(self, channels):  # 按数据源的通道定义重建缓冲区
        """按数据源的通道定义重建缓冲区。"""
        self.channels = [dict(channel) for channel in channels]  # 拷贝通道定义
        self.history = {channel['name']: deque(maxlen=WINDOW_MAX) for channel in self.channels}  # 每通道一个定长队列
        self.stamps.clear()  # 清空时间标签
        self.tick = 0  # 采样序号归零

    def _source_label(self, source):  # 由数据源对象反查显示名
        for label, item in self.source_registry:  # 遍历注册表
            if item is source:  # 找到同一个对象
                return label
        return '数据源'  # 找不到时的兜底名称

    def _use_source(self, source, label):  # 切换数据源：关旧、开新、重建缓冲
        """切换数据源：关闭旧的、加载新通道、重铺历史。"""
        if source is self.source:  # 就是当前源：只更新显示名
            self.current_label = label
            return
        try:
            self.source.close()  # 关闭旧数据源的资源
        except Exception:  # close 失败不阻塞切换
            pass
        self.source = source  # 换成新数据源
        self.current_label = label
        try:
            self.source.open()  # 打开新数据源的资源
        except Exception:  # open 失败留给 poll() 再报错
            pass
        self._load_channels(self.source.channels())  # 按新通道重建缓冲区
        if hasattr(self, 'chart'):  # 界面已建好才重铺历史
            self.rebuild_readout()  # 重建读数标签
            self.seed(SEED_POINTS)  # 预填一段历史
            self.refresh()  # 立即重绘

    def select_source(self, label):  # 按显示名切换数据源
        for name, source in self.source_registry:  # 在注册表里查找
            if name == label:  # 找到了
                self._use_source(source, label)  # 执行切换
                self.source_var.set(label)  # 同步下拉框显示
                notify(self, f'数据源：{label}', kind='info')  # toast 提示
                return

    def on_source_change(self, event=None):  # 下拉框选中事件
        self.select_source(self.source_var.get())

    def register_source(self, label, source):  # 运行期注册数据源（同名则替换）
        """运行期注册新的数据源（会出现在下拉框中）；同名则替换。"""
        for index, (name, _) in enumerate(self.source_registry):  # 先找同名项
            if name == label:
                self.source_registry[index] = (label, source)  # 原地替换
                break
        else:  # 没有同名则追加到末尾
            self.source_registry.append((label, source))
        self.source_combo.configure(values=[name for name, _ in self.source_registry])  # 刷新下拉框选项

    # ---------- 真实设备 ----------
    def open_device_dialog(self):  # 弹出设备配置对话框，连接串口 / Modbus 等真实设备
        """弹出设备配置对话框，连接串口 / Modbus 等真实设备。"""
        try:
            from devices.dialog import ask_device  # 延迟导入：devices 包可能不存在
        except Exception as exc:  # 缺少 tkwebui 或包路径
            notify(self, f'无法打开设备对话框：{exc}', kind='danger')  # 给出失败原因
            return
        device = ask_device(self, self.device)  # 模态对话框：返回配置好的 Device 或 None
        if device is not None:  # 用户点了“确定”
            self.connect_device(device)  # 立即连接

    def connect_device(self, device):  # 把真实设备接成数据源：后台线程读设备 → PushDataSource → 主线程绘图
        """把真实设备接成数据源：后台线程读设备 → PushDataSource → 主线程绘图。"""
        if self.capture is not None and self.capture.is_alive():  # 先停掉正在跑的采集线程
            self.stop_capture()
        try:
            device.open()  # 打开设备（串口 / Modbus / socket 等）
        except Exception as exc:  # 打不开就提示并放弃
            notify(self, f'连接设备失败：{exc}', kind='danger')
            return
        channels = device.chart_channels()  # 设备声明要画哪些通道
        if not channels:  # 没有通道就没法画图
            device.close()  # 关掉刚打开的设备
            notify(self, '设备没有定义任何通道', kind='danger')
            return
        source = PushDataSource(channels)  # 设备数据走推送式数据源
        label = '设备：' + device.describe()  # 下拉框里的显示名
        self.device = device  # 记住设备，退出时统一关闭
        self.register_source(label, source)  # 注册进数据源注册表
        self._use_source(source, label)  # 切换到设备数据源
        self.source_var.set(label)  # 下拉框同步显示
        self.capture = CaptureThread(
            source, channels, rate=self.capture_rate.get(), reader=device.read
        )  # 采集线程读真实设备
        self.capture.start()  # 后台开始采集
        self._last_error = None  # 清空上一次的错误提示
        self.capture_button.configure(text='停止采集')  # 按钮变为可停止
        if not self.running:  # 保证曲线会动
            self.start()
        self.status.set(f'已连接 {device.describe()} · 采集线程 {int(self.capture_rate.get())} Hz')  # 状态栏反馈
        notify(self, f'已连接：{device.describe()}', kind='success')  # toast 提示

    def _close_device(self):  # 关闭并清空当前设备
        if self.device is not None:
            try:
                self.device.close()  # 释放串口 / 网络等资源
            except Exception:  # 关闭失败不阻塞退出
                pass
            self.device = None

    def push_data(self, name, value):  # 对外入口：把真实数据推入采集源
        """对外入口：把真实数据推入采集源（可从任意线程调用）。"""
        self.push_source.push(name, value)  # 线程安全，任意线程可调

    # ---------- 采集线程 ----------
    def toggle_capture(self):  # 启动 / 停止采集线程
        if self.capture is not None and self.capture.is_alive():  # 正在跑就停
            self.stop_capture()
        else:  # 没跑就启动
            self.start_capture()

    def start_capture(self):  # 启动后台采集线程（默认模拟数据）
        """启动后台采集线程：它只往 PushDataSource 写，界面由主线程刷新。"""
        if self.capture is not None and self.capture.is_alive():  # 切换数据源前先停掉旧的采集 / 设备线程
            self.stop_capture()  # 切换数据源前先停掉旧的采集 / 设备线程
        self._close_device()  # 切回推送源前先关设备
        label = self._source_label(self.push_source)  # 推送源的显示名
        self._use_source(self.push_source, label)  # 切换到推送源
        self.source_var.set(label)  # 下拉框同步
        if not self.running:  # 主线程没在刷新时自动开始，保证能显示
            self.start()
        self.capture = CaptureThread(self.push_source, SIGNALS, rate=self.capture_rate.get())  # 模拟采集线程
        self.capture.start()  # 后台开始采集
        self._last_error = None  # 清空旧错误
        self.capture_button.configure(text='停止采集')  # 按钮变为可停止
        self.status.set(
            f'采集线程运行中：{int(self.capture_rate.get())} Hz → PushDataSource（主线程轮询显示）'
        )  # 状态栏说明数据流
        notify(self, '采集线程已启动', kind='success')  # toast 提示

    def stop_capture(self):  # 停止采集线程并关闭设备
        active = self.capture is not None or self.device is not None  # 是否真的在运行
        if self.capture is not None:
            self.capture.stop()  # 请求线程退出
            self.capture.join(timeout=1.0)  # 最多等 1 秒
            self.capture = None
        self._close_device()  # 释放设备资源
        self.capture_button.configure(text='启动采集')  # 按钮复位
        if active:  # 之前确实在跑才提示
            notify(self, '采集线程已停止', kind='neutral')

    def on_rate_change(self, value):  # 运行中调整采集频率（滑块拖动触发）
        """运行中调整采集频率（线程下一轮生效）。"""
        if self.capture is not None and self.capture.is_alive():
            self.capture.rate = float(value)  # 线程下一轮生效

    # ---------- 数据 ----------
    def series(self, count=None):  # 按当前窗口长度组装 LineChart 需要的多系列数据
        """按当前窗口长度组装 LineChart 需要的多系列数据。"""
        count = count or max(2, int(self.window.get()))  # 默认取滑块设定的窗口长度
        return [
            {'name': channel['name'], 'color': channel['color'], 'values': list(self.history[channel['name']])[-count:]}
            for channel in self.channels  # 每个通道取最近 count 个点
        ]

    def collect(self):  # 从数据源取一批样本写入缓冲区
        """从数据源取一批样本写入缓冲区，返回是否有新数据。"""
        batch = self.source.poll()  # 取一次数据（可能为 None）
        if not batch:  # 没有新数据
            return False
        self.tick += 1  # 采样序号推进一格
        self.stamps.append(datetime.now().strftime('%H:%M:%S'))  # 记录对应的时间标签
        for channel in self.channels:  # 逐通道写入
            self.history[channel['name']].append(batch.get(channel['name']))  # 缺的写 None（折线断开）
        return True

    def seed(self, points):  # 预取若干样本，让窗口一打开就有曲线
        """预取若干样本，让窗口一打开就有曲线；没有数据的源会自动跳过。"""
        batches = []  # 先取到内存，再统一铺时间轴
        for _ in range(points):  # 最多取 points 批
            batch = self.source.poll()
            if not batch:  # 数据源给不出更多就提前结束
                break
            batches.append(batch)
        if not batches:  # 一批都没取到
            return
        step = max(1, int(self.interval.get())) / 1000.0  # 相邻两点的假设时间间隔（秒）
        now = datetime.now()  # 以当前时间为终点往回推
        for offset, batch in enumerate(batches):  # 逐批写入缓冲区
            self.tick += 1
            seconds = (len(batches) - offset) * step  # 越早的批次时间越靠前
            self.stamps.append((now - timedelta(seconds=seconds)).strftime('%H:%M:%S'))  # 回推出的时间标签
            for channel in self.channels:
                name = channel['name']
                self.history[name].append(batch.get(name))  # 缺的写 None

    def refresh(self):  # 把缓冲区的最新数据交给图表重绘（只在主线程调用）
        """把缓冲区的最新数据交给图表重绘（只在主线程调用）。"""
        if not hasattr(self, 'chart'):  # 界面还没建好
            return
        count = max(2, int(self.window.get()))  # 窗口长度
        labels = list(self.stamps)[-count:]  # 与数据对应的时间标签
        self.chart.set_data(series=self.series(count), labels=labels, reset_view=self.auto_scale.get())  # 重绘
        self.update_readout()  # 同步底部读数

    def update_readout(self):  # 更新底部每个通道的最新值
        for channel, label in zip(self.channels, self.readout_labels):  # 通道与标签一一对应
            values = self.history[channel['name']]
            if values and values[-1] is not None:  # 有新值：显示两位小数
                text = f"● {channel['name']}  {values[-1]:.2f}"
            else:  # 没有数据显示 --
                text = f"● {channel['name']}  --"
            label.configure(text=text)

    # ---------- 定时刷新 ----------
    def start(self):  # 开始刷新循环
        if self.running:  # 已经在跑
            return
        self.running = True
        self.toggle_button.configure(text='暂停')  # 按钮变为“暂停”
        self.tick_once()  # 立即执行一轮

    def pause(self):  # 暂停刷新循环
        if not self.running:  # 本来就停着
            return
        self.running = False
        self.toggle_button.configure(text='开始')
        if self.job is not None:  # 取消已排定的下一轮
            self.after_cancel(self.job)
            self.job = None

    def toggle(self):  # 暂停 / 开始 切换
        if self.running:
            self.pause()
            self.status.set('已暂停刷新；采集线程仍可继续写入数据')  # 说明采集不受影响
            notify(self, '已暂停刷新', kind='neutral')
        else:
            self.start()
            notify(self, '开始刷新', kind='success')

    def tick_once(self):  # 主循环一轮：取数据并重绘，再排定下一轮
        """主线程循环：取出采集线程写入的数据并重绘。"""
        self.job = None  # 本轮已开始执行
        if self.capture is not None and self.capture.error:  # 采集线程报错了
            error = self.capture.error
            self.capture.error = None  # 取走后清空，避免重复提示
            if error != getattr(self, '_last_error', None):  # 同一条错误只提示一次
                notify(self, '采集线程出错：' + error, kind='danger')
            self._last_error = error
        self.collect()  # 取数据写入缓冲区
        self.refresh()  # 重绘
        if self.capture is not None and self.capture.is_alive():  # 采集在跑时显示运行参数
            self.status.set(
                f'采集线程 {int(self.capture_rate.get())} Hz · 主线程每 '
                f'{int(self.interval.get())} ms 刷新 · 显示最近 {int(self.window.get())} 点'
            )
        if self.running:  # 还在运行才排定下一轮
            self.job = self.after(int(self.interval.get()), self.tick_once)

    # ---------- 操作 ----------
    def clear(self):  # 清空所有缓冲数据
        for buffer in self.history.values():  # 每个通道的历史
            buffer.clear()
        self.stamps.clear()  # 时间标签也清空
        self.tick = 0  # 序号归零
        self.refresh()  # 立即重绘为空图
        notify(self, '已清空数据', kind='neutral')

    def reset_view(self):  # 恢复自动缩放与初始视图
        self.auto_scale.set(True)  # 打开自动缩放
        self.chart.reset_view()  # 图表恢复默认坐标范围

    def tooltip(self, data):  # 悬浮提示文本：系列名 + 时间 + 值
        value = data.get('y', data.get('value', ''))  # 兼容两种字段名
        try:
            value = f'{float(value):.2f}'  # 数值保留两位小数
        except (TypeError, ValueError):  # 非数值原样显示
            value = str(value)
        return f"{data.get('series', data.get('name', ''))}\n{data.get('label', '')}  {value}"

    def on_point(self, data):  # 点击数据点：在状态栏显示详情
        value = data.get('y', data.get('value', ''))
        try:
            value = f'{float(value):.2f}'  # 数值保留两位小数
        except (TypeError, ValueError):  # 非数值原样显示
            value = str(value)
        self.status.set(f"点击：{data.get('series', '')} · {data.get('label', '')} · {value}")  # 状态栏反馈

    def close(self):  # 退出：停线程、关设备、销毁窗口
        self.running = False
        if self.job is not None:  # 取消排定的下一轮
            self.after_cancel(self.job)
            self.job = None
        if self.capture is not None:  # 先停后台线程，再销毁界面
            self.capture.stop()  # 请求线程退出
            self.capture.join(timeout=1.0)  # 最多等 1 秒
            self.capture = None
        self._close_device()  # 释放串口 / Modbus 连接
        try:
            self.source.close()  # 关闭数据源占用的资源
        except Exception:
            pass
        self.destroy()  # 销毁窗口，mainloop() 返回


def main(source=None, device=None):  # 启动示例；可传自定义数据源或真实设备
    """启动示例。传入 source 可用自定义数据源；传入 device 可直接连接真实设备。"""
    enable_dpi_awareness()  # 必须在创建 Tk() 之前调用
    LiveChartApp(source=source, device=device).mainloop()  # 创建窗口并进入事件循环


if __name__ == '__main__':  # 直接运行本文件时才启动
    main()
