# 项目梳理：实时数据流与逻辑

本文梳理 tkwebui 项目里"实时数据采集 → 曲线显示"这条链路的**数据流、模块职责与关键逻辑**，作为后续扩展（新增数据源 / 设备 / 协议）的参考。

适用代码：

- `tkwebui/`：通用 Web 风格控件库（纯标准库，零依赖）。
- `examples/live_chart.py`：实时动态折线图示例（主界面）。
- `examples/devices/`：可扩展的设备 / 协议层（串口、Modbus、TCP/UDP、HTTP、MQTT、OPC UA、CAN、SNMP）。
- `examples/devices/dialog.py`：设备配置对话框。
- `uv_Setup部署.md`：环境与依赖说明。

---

## 1. 总览

```
┌───────────────────────────── 示例层 examples/ ─────────────────────────────┐
│                                                                            │
│   live_chart.py                                  devices/                  │
│   ┌───────────────────────┐                ┌──────────────────────────┐    │
│   │ LiveChartApp (Tk 主线程)│  connect_device │ DeviceDialog (配置对话框) │    │
│   │  - after 定时器         │ <──────────────│  - 协议 / 参数 / 通道表    │    │
│   │  - 缓冲 / 重绘          │                └──────────────────────────┘    │
│   └───────────┬───────────┘                            │ create()           │
│               │ poll()                                 ▼                    │
│        ┌──────┴───────┐                        ┌──────────────┐             │
│        │  DataSource   │◀──── push() ──────────│  Device 实例  │             │
│        │  (接口)       │   线程安全队列          │ open/read/   │             │
│        │ PushDataSource│                        │ close        │             │
│        └──────▲───────┘                        └──────▲───────┘             │
│               │ push()                                 │ read()             │
│        ┌──────┴───────────────────────────────────────┴───────┐            │
│        │  CaptureThread / 后台采集线程（非 Tk 线程）             │            │
│        └──────────────────────────────────────────────────────┘            │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
                                    │ 只调用 set_data()
                                    ▼
┌───────────────────────────── 控件层 tkwebui/ ──────────────────────────────┐
│  LineChart / _CartesianChart   ·   ThemedCombobox / ValueSlider / notify   │
│  Spreadsheet（设备通道表）      ·   ResultDialog（对话框基类）             │
└────────────────────────────────────────────────────────────────────────────┘
```

**核心原则：**

1. 采集在后台线程，绘图在主线程；两者只通过 `PushDataSource` 的线程安全队列通信。
2. 采集线程**绝不触碰任何 Tk 控件**，只调用 `push()`。
3. 界面帧率与采集频率解耦：采集按 Hz，重绘按毫秒定时器。
4. 设备 / 协议与界面解耦：新增协议不改图表代码。

---

## 2. 端到端数据流

### 2.1 三种数据来源，同一个出口

| 来源 | 类 | poll() 行为 | 典型用途 |
|---|---|---|---|
| 内置伪数据 | `FakeDataSource` | 每次生成正弦+漂移+抖动 | 演示、预填历史 |
| 外部推送（采集线程） | `PushDataSource` | 从队列取每通道最新值 | 真实数据入口 |
| 自定义回调 | `CallbackDataSource` | 调用外部 `reader()` | 文件 / 数据库 / 简单函数 |
| 真实设备 | `PushDataSource` + `Device.read()` | 线程把设备读数 push 进队列 | 串口 / Modbus / TCP / UDP / HTTP / MQTT / OPC UA / CAN / SNMP |

无论哪种来源，主界面都只做同一件事：**`poll()` 取一批数据 → 写入环形缓冲 → `set_data()` 重绘**。

### 2.2 主循环（主线程 · tkinter `after` 定时器）

```
LiveChartApp.start()
   └─ tick_once()  ──────────────────────────────── 每 interval 毫秒循环一次
        ├─ 检查 capture.error          → 出错时弹一次 toast（同一条错误去重）
        ├─ collect()                   → batch = source.poll()
        │       └─ 有新数据才：tick+1、追加时间戳、每通道追加一个样本
        ├─ refresh()                   → chart.set_data(series, labels, reset_view=auto_scale)
        ├─ update_readout()            → 底部读数标签刷新
        └─ after(interval, tick_once)  → 继续下一次
```

- `interval`：刷新间隔，默认 **300 ms**（滑块 100–2000 ms）。
- `window`：显示窗口点数，默认 **60 点**（滑块 20–200 点）。
- `WINDOW_MAX = 400`：每个通道缓冲区上限（`deque(maxlen=400)`）。
- `SEED_POINTS = 40`：启动时预取的历史点数，界面一打开就有曲线。

### 2.3 采集线程（后台 · 非 Tk 线程）

```
CaptureThread.run()  ─────────────────────────── 循环直到 _stop_event 被 set
   ├─ batch = reader()                  → 返回 {通道名: 数值} 或 None
   │       ├─ 伪数据模式：DriftingSignal 生成
   │       └─ 设备模式：reader = device.read
   ├─ for name, value in batch: source.push(name, value)
   ├─ 出错：self.error = str(exc)，等待后重试（不退出、不抛到主线程）
   └─ 按 rate(Hz) 节流后等待
```

- `rate`：采集频率，默认 **20 Hz**（滑块 1–100 Hz），运行中可改（下一轮生效）。
- `stop()` 通过 `Event` 通知；用 `self._stop_event` 命名（`Thread` 已有私有 `_stop` 方法，不能占用）。
- 采集频率 ≠ 重绘频率：20 Hz 采集、300 ms 重绘时，队列里同一通道的多个样本只保留最后一个（实时曲线只关心最新值）。

---

## 3. 核心抽象（`examples/live_chart.py`）

### 3.1 `DataSource` —— 数据源接口

```python
class DataSource:
    def channels(self): ...   # [{'name': 通道名, 'color': '#RRGGBB'}, ...]
    def poll(self):   ...     # {通道名: 数值} 或 None；值为 None 表示断点
    def open(self):   pass    # 可选：连接资源
    def close(self):  pass    # 可选：释放资源
```

接入自定义来源只需实现 `channels()` + `poll()`，再 `main(source=MySource())`。

### 3.2 `PushDataSource` —— 线程安全入口

- `push(name, value)`：可从任意线程调用，写入 `queue.Queue`。
- `poll()`：取空队列，返回 `{通道名: 最新值}`；无数据返回 `None`。
- 同通道多样本只保留最后一个（实时语义）。

### 3.3 `FakeDataSource` / `FakeSensor` —— 伪数据

`sensor.value(tick) = base + amp * (0.6*正弦 + 0.4*长周期漂移) + noise*抖动`，抖动用可复现的哈希噪声。

### 3.4 `DriftingSignal` —— 线程内伪数据

`FakeDataSource` 的线程版：随机游走 + 向基线回归，供"启动采集"演示后台线程写入。

### 3.5 `CaptureThread`

```python
CaptureThread(source, specs=SIGNALS, rate=20, reader=None)
```

- `specs` 里带 `base` 的通道才生成 `DriftingSignal`（设备通道无 `base`，不会误建）。
- `reader` 缺省为伪数据 `_read()`；设备模式传入 `device.read`。

---

## 4. 界面逻辑（`LiveChartApp`）

### 4.1 状态字段

| 字段 | 含义 |
|---|---|
| `source` / `source_registry` | 当前数据源；`[(标签, 源), ...]` 用于下拉框 |
| `builtin_source` / `push_source` | 内置伪数据源 / 外部推送源 |
| `channels` | 当前通道定义 |
| `history` | `{通道名: deque(maxlen=400)}` 环形缓冲 |
| `stamps` | 时间戳 `deque`，与样本一一对应 |
| `tick` | 已产生样本序号 |
| `job` | `after` 定时器句柄 |
| `capture` | 当前后台采集线程 |
| `device` | 当前已连接的真实设备（有采集线程时非空） |
| `running` | 主循环是否在刷新 |
| `_last_error` | 最近一次采集错误文本（用于 toast 去重） |

### 4.2 关键方法

| 方法 | 职责 |
|---|---|
| `build()` | 搭建工具栏 / 参数区 / 读数区 / `LineChart` |
| `_load_channels(c)` | 按通道定义重建 `history` 与 `stamps` |
| `_use_source(source, label)` | 关闭旧源、切换、`open()` 新源、重铺历史并刷新 |
| `register_source(label, source)` | 运行期注册数据源到下拉框（同名替换） |
| `collect()` | `poll()` 一批并按通道写入缓冲 |
| `seed(n)` | 启动时预取 n 个样本，回填时间戳 |
| `refresh()` | `chart.set_data(series, labels, reset_view=auto_scale)` |
| `tick_once()` / `start()` / `pause()` / `toggle()` | 主循环与暂停/继续 |
| `toggle_capture()` / `start_capture()` / `stop_capture()` | 采集线程启停 |
| `on_rate_change(v)` | 运行中调整采集频率 |
| `open_device_dialog()` / `connect_device(d)` / `_close_device()` | 设备连接与生命周期 |
| `clear()` / `reset_view()` | 清空缓冲 / 复位视图 |
| `close()` | 停线程、关设备、取消定时器后销毁窗口 |

### 4.3 数据源切换与设备连接

- 下拉框切换 → `select_source(label)` → `_use_source(...)`：关闭旧源、加载新通道、`seed()` 预填、`refresh()`。
- 「启动采集」→ 切到 `push_source`，新建 `CaptureThread` 写入伪数据；若当前是设备源，会先停止并关闭设备。
- 「连接设备…」→ 弹出 `DeviceDialog`，确认后 `connect_device(device)`：

```
connect_device(device)
   ├─ 若已有采集线程：stop_capture()（停线程 + 关旧设备）
   ├─ device.open()                       → 失败弹 toast 并返回
   ├─ channels = device.chart_channels()  → 空通道则关闭并提示
   ├─ source = PushDataSource(channels)
   ├─ self.device = device
   ├─ register_source('设备：' + describe, source)
   ├─ _use_source(source, label)          → 切换 + 重置缓冲
   └─ CaptureThread(source, channels, rate, reader=device.read).start()
```

- 停止采集 / 切换来源 / 关闭窗口时都会 `_close_device()`，释放串口或 Modbus 连接。

### 4.4 图表刷新参数

```python
chart.set_data(series=series(count), labels=labels, reset_view=auto_scale.get())
```

- `series`：`[{'name', 'color', 'values': [...]}, ...]`，取缓冲末尾 `count = max(2, int(window))` 个点。
- `labels`：对应时间戳（`HH:MM:SS`）。
- `reset_view=True` 时每帧重新适配坐标范围（自动缩放开关）。

---

## 5. 线程模型与安全规则

| 线程 | 允许做的事 | 禁止做的事 |
|---|---|---|
| 主线程（Tk） | 所有 Tk 控件操作、`poll()`、`set_data()`、`push()` | —— |
| 采集线程 | `source.push(name, value)`、设备 `read()` | 任何 Tk 调用、更新控件变量 |

规则：

1. **单向数据流**：采集线程 → 队列 → 主线程。队列由 `queue.Queue` 保证线程安全。
2. **错误传递**：采集线程把异常存到 `capture.error`，主线程 `tick_once()` 读取并弹 toast，同一条错误只提示一次。
3. **停止顺序**：先 `stop()` + `join()` 线程，再关设备/销毁界面。
4. **节流**：同一个 `after` 循环内完成"取数据 + 重绘"，不会重入（`start()` 有 `running` 守卫）。

---

## 6. 设备 / 协议层（`examples/devices/`）

### 6.1 `base.py` —— 抽象与注册表

- `Channel`（数据类）：一条通道既描述曲线，又描述它在设备里的位置。

  | 字段 | 说明 |
  |---|---|
  | `name` | 图表显示名，也是数据字典键 |
  | `color` | 曲线颜色 |
  | `field` | 文本寻址：报文字段名 / MQTT 主题 / JSON 路径 / OID / NodeId（留空用 name） |
  | `address` | 数字寻址：Modbus 寄存器地址 / CAN 帧 ID |
  | `start` | CAN：帧内数据字节偏移（配合 address 表示帧 ID） |
  | `dtype` | Modbus 与 CAN 的数值类型 |
  | `function` | Modbus：holding/input/coil（留空用设备默认） |
  | `scale` / `offset` | 显示值 = 原始值 × scale + offset |
  | `unit` | 仅界面提示 |

- `Option`（数据类）：一条连接参数的界面描述（`key/label/kind/default/choices/help/editable`），供自动生成表单。
- `Device`（基类）：`open/close/read` + `chart_channels()/describe()` + `opt/opt_int/opt_float` 取值工具。
- 注册表：`register(cls)`（装饰器）、`protocols()`、`create(protocol, channels, **options)`。

### 6.2 `serial_dev.py` —— 串口（pyserial）

- 打开：`port` 含 `://` 用 `serial.serial_for_url`，否则 `serial.Serial`；支持 `COM3`、`socket://host:port`、`loop://`。
- 读取：`readline()` → 去空白 → 解析 → 每通道换算 → `{name: value}`。
- 解析模式：
  - `keyvalue`：按 `separator`(默认`,`) 切分，再按 `assignment`(默认`=`) 取键值，如 `T=25.6,P=101.2`。
  - `csv`：按列顺序对应通道，如 `25.6,101.2`。
- 缺字段 / 非法数字用 `to_float()` 转 `None` 并跳过，不报错。

### 6.3 `modbus_dev.py` —— Modbus（pymodbus）

- 链路：`mode=tcp` 用 `ModbusTcpClient`；`mode=rtu` 用 `ModbusSerialClient`（新版 `framer=`，旧版回退 `method='rtu'`）。
- 读取流程：

```
read()
 ├─ 按 function 分组通道
 └─ 每组：_merge() 把地址相邻/重叠的通道合并成连续区间
        └─ _request() 一次读整段（带 device_id/slave 关键字兼容回退）
             └─ 每通道按 dtype 从区间里切片，_decode() 解成数值
                  └─ 应用 scale/offset，写回 {name: value}
```

- 解码 `_decode(registers, dtype, word_order)`：
  - 16 位：单寄存器 `>H` / `>h`；`bool` 取非零。
  - 32 位：两寄存器按 `word_order`（big/little 高字在前与否）拼字节，`>I/>i/>f` 解包。
- 线圈：`function='coil'` 读 `.bits`，返回 0.0 / 1.0。
- 越界 / 异常响应：`isError()` 为真则跳过该段，不抛异常（保持曲线连续）。

支持的 Modbus 参数：`mode/host/port/serial_port/baudrate/parity/unit/timeout/function/word_order`。

### 6.4 其它内置协议（TCP/UDP、HTTP、MQTT、OPC UA、CAN、SNMP）

除串口 / Modbus 外还内置 7 种常用协议，`Device` 用类属性声明通道语义，对话框据此生成提示并解析通道行：

| 协议 | 模块 | 链路 | 「字段/地址」列 | 依赖 |
|---|---|---|---|---|
| TCP | `socket_dev.py` | 原始 TCP 行报文 | 报文字段名 | 无（stdlib） |
| UDP | `socket_dev.py` | UDP 数据报 | 报文字段名 | 无（stdlib） |
| HTTP | `http_dev.py` | GET / POST JSON | JSON 路径（`a.b.0`） | 无（stdlib） |
| MQTT | `mqtt_dev.py` | 订阅主题、缓存最新消息 | 主题 / 字段名 / JSON 路径 | paho-mqtt |
| OPC UA | `opcua_dev.py` | asyncua 同步客户端读节点 | NodeId（`ns=2;i=2`） | asyncua |
| CAN | `can_dev.py` | python-can 收帧后解码 | 帧 ID（`0x123` 或 `0x123:偏移`） | python-can |
| SNMP | `snmp_dev.py` | 纯标准库实现 v1/v2c GET | OID | 无（stdlib） |

- **TCP / UDP**：报文解析复用 `util.parse_line` 的 keyvalue / csv 格式。TCP 可先发一条查询指令再读一行回复；UDP 可只监听（如广播流）或发查询后收一条回复。
- **HTTP**：按 JSON 路径取值；用「请求头 / 请求体 / 取值字段」适配常见 REST 接口。
- **MQTT**：`mode=value` 时每通道一个主题、payload 是数值；`mode=keyvalue` / `json` 时所有通道共用一个「公共主题」，分别按报文字段名 / JSON 路径取值。paho 在后台跑网络循环，`read()` 只读缓存的最新消息。
- **OPC UA**：用 `asyncua.sync.Client`，可直接在采集线程里逐节点读取，无需自管事件循环。
- **CAN**：`read()` 把总线上已有帧全部取走，按帧 ID 保留最新一帧；`dtype`（如 `uint16_be` / `float32_le`）决定宽度与字节序，`address` + `start` 指定帧 ID 与字节偏移。
- **SNMP**：`snmp_dev.py` 自带极简 BER 编解码，只做 GET（单个 OID）；整数 / 计数器 / 计时器 / 字符串数字都会转成数值。
- 未安装可选依赖时协议仍会出现在下拉框，`open()` 时给出 `uv pip install xxx` 的提示。

`Device` 中与对话框相关的类属性：

| 类属性 | 含义 |
|---|---|
| `channel_kind` | `field` 文本寻址 / `address` 数字寻址 / `can` 帧 ID（支持 `:偏移`） |
| `address_radix` | `address` 寻址时的进制（Modbus 10，CAN 16） |
| `dtypes` | 「类型」列允许的取值；空元组表示该列忽略 |
| `channel_hint` | 「字段/地址」列的界面提示文字 |

### 6.5 `dialog.py` —— 设备配置对话框

- `DeviceDialog(ResultDialog)`：标题「连接真实设备」，返回一个**未连接**的 `Device`。
- 结构：协议下拉 → 连接参数区（按 `option_fields()` 自动生成）→ 采集通道 `Spreadsheet`。
- 通道表列：`名称 | 颜色 | 字段/地址 | 类型 | 系数 | 偏移`；具体填法随协议而定（见 §6.4 的 `channel_kind` / `channel_hint`）。
- 切换协议时通道表恢复为该协议的示例行，避免字段名 / 地址混用。
- `build_device()`：把表单 + 通道表组装成 `create(protocol, channels, **options)`。
- `test_read()`：临时打开设备读一次并关闭，成功/失败都用 `notify` 提示。
- `ask_device(master, device=None)`：`show_result()` 包装，返回 `Device` 或 `None`。

### 6.6 内置协议参数一览

| 协议 | 关键参数 |
|---|---|
| serial | `port`(可手输) / `baudrate` / `bytesize` / `parity` / `stopbits` / `timeout` / `mode` / `separator` / `assignment` / `encoding` |
| modbus | `mode`(tcp/rtu) / `host` / `port` / `serial_port` / `baudrate` / `parity` / `unit` / `timeout` / `function` / `word_order` |
| tcp | `host` / `port` / `timeout` / `query` / `line_end` / `mode` / `separator` / `assignment` / `encoding` |
| udp | `host` / `port` / `bind_port` / `broadcast` / `timeout` / `query` / `mode` / `separator` / `assignment` / `encoding` |
| http | `url` / `method` / `headers` / `body` / `value_key` / `timeout` |
| mqtt | `host` / `port` / `client_id` / `username` / `password` / `tls` / `keepalive` / `mode` / `topic` |
| opcua | `url` / `username` / `password` / `timeout` |
| can | `interface` / `channel` / `bitrate` / `timeout` |
| snmp | `host` / `port` / `community` / `version` / `timeout` / `retries` |

---

## 7. 扩展点

### 7.1 新增数据源

实现 `DataSource.channels()` + `poll()`，然后：

```python
main(source=MySource())        # 或 app.register_source('我的源', MySource())
```

### 7.2 新增设备 / 协议（不改界面与图表）

```python
# examples/devices/mybus_dev.py
from .base import Channel, Device, DeviceError, Option, register

@register
class MyBusDevice(Device):
    protocol = 'mybus'
    label = 'MyBus'

    @classmethod
    def option_fields(cls):
        return [Option('host', '地址', 'text', default='127.0.0.1')]

    def open(self):  self._link = open_link(self.opt('host'))
    def close(self): self._link.close()
    def read(self):
        return {c.name: read_point(c.field) * c.scale + c.offset for c in self.channels}
```

再在 `examples/devices/__init__.py` 里 `from . import mybus_dev` 一次即可。
配置对话框会依据 `option_fields()` 自动出现表单，下拉框、采集线程、图表全部自动生效。
若通道用数字地址 + 类型描述（如 Modbus / CAN），在子类上声明 `channel_kind` / `address_radix` /
`dtypes` / `channel_hint`，对话框会按对应规则解析通道行。

---

## 8. 运行与依赖

```powershell
# 运行实时曲线示例（推荐直接用虚拟环境解释器）
.\.venv\Scripts\python.exe examples\live_chart.py

# 设备依赖（全部可选；TCP/UDP/HTTP/SNMP 用纯标准库，无需安装）
uv pip install pyserial pymodbus paho-mqtt python-can asyncua
# 或  pip install pyserial pymodbus paho-mqtt python-can asyncua
```

| 协议 | 依赖 |
|---|---|
| serial | `pyserial` |
| modbus | `pymodbus` |
| tcp / udp / http / snmp | 无（纯标准库） |
| mqtt | `paho-mqtt` |
| opcua | `asyncua` |
| can | `python-can` |

- Python 3.11（`.python-version` / `pyproject.toml`）。
- 托盘示例 `examples/live_chart_tray.py` 用 ctypes 调 Win32 实现托盘，纯标准库，无需额外依赖。
- `uv run` / `uv sync` 在无法访问 PyPI 的环境会因解析构建依赖失败，此时用 `.venv` 解释器直接运行。
- 设备依赖未安装时，程序仍可运行；选中设备协议并与硬件交互时才会提示缺少库。
- 详细环境步骤见 `uv_Setup部署.md`。

---

## 9. 文件清单

| 文件 | 职责 |
|---|---|
| `examples/live_chart.py` | 主界面：数据源抽象、采集线程、实时折线图、设备接入 |
| `examples/live_chart_tray.py` | 托盘版主界面：启动隐藏到托盘、右键菜单（继承 LiveChartApp） |
| `examples/trayicon.py` | 纯标准库 Windows 托盘图标（ctypes + Shell_NotifyIcon） |
| `examples/devices/__init__.py` | 导入即注册内置协议（import 顺序 = 下拉框顺序） |
| `examples/devices/base.py` | `Device`/`Channel`/`Option` + 注册表 + 通道语义类属性 |
| `examples/devices/util.py` | 共用工具：文本/报文解析、JSON 路径、地址解析 |
| `examples/devices/serial_dev.py` | 串口协议实现 |
| `examples/devices/modbus_dev.py` | Modbus 协议实现 |
| `examples/devices/socket_dev.py` | TCP / UDP 原始套接字 |
| `examples/devices/http_dev.py` | HTTP / JSON |
| `examples/devices/mqtt_dev.py` | MQTT（paho-mqtt） |
| `examples/devices/opcua_dev.py` | OPC UA（asyncua） |
| `examples/devices/can_dev.py` | CAN / CANopen（python-can） |
| `examples/devices/snmp_dev.py` | SNMP v1/v2c（纯标准库） |
| `examples/devices/dialog.py` | 设备配置对话框 |
| `tkwebui/charts.py` | `LineChart` / `set_data` 等图表能力 |
| `tkwebui/table.py` | `Spreadsheet`（设备通道表） |
| `tkwebui/forms.py` | `ResultDialog` / `ValueSlider` 等 |
| `uv_Setup部署.md` | 环境 / 依赖说明 |

---

## 10. 已验证场景

- 伪数据：启动预填历史 + 启动采集线程写入（缓冲增长、线程可正常停止）。
- 串口：报文解析（keyvalue / csv / 缺列容错）；经 `socket://` 真实读写路径 + 系数换算。
- Modbus：uint16 / int16 / uint32 / float32 / 线圈读取、字序切换、系数偏移、越界跳过。
- TCP：本地回环服务端「查询指令 + 行回复」；UDP：发查询后收一条数据报。
- HTTP：本地 JSON 接口按 `data.temp` 路径取值。
- CAN：`virtual` 总线收发 + `uint16_be` / `int16_le` 解码、字节偏移。
- OPC UA：本地 `asyncua` 服务端，读节点值并在更新后重读。
- SNMP：BER 编解码往返（OID 编码与 INTEGER 解析）。
- MQTT：`value` / `keyvalue` / `json` 三种取值逻辑（缓存快照）。
- 界面集成：对话框在 9 种协议下都能用示例行构建设备；图表接收到真实的 Modbus 返回值；切回模拟采集时设备被正确关闭。

---

## 11. 后续可做（备忘）

- 多设备并行采集（当前同一时刻只运行一个采集线程）。
- 设备配置持久化（保存到 JSON / 配置文件，启动时直接恢复）。
- 采集数据落盘（CSV / 数据库）与历史回放。
- 更多协议：Profinet / EtherNet/IP / BACnet、VISA/SCPI 仪器、BLE 等（多数需经网关或额外依赖）。
- 通道表支持直接从设备读取点位表（导入 CSV / Excel）。
