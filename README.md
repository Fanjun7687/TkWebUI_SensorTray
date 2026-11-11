<div align="center">

# TkWebUI SensorTray

**基于 [tkwebui](../tkwebui) 的传感器实时曲线监控工具 —— 系统托盘常驻 · 9 种工业协议接入 · 纯 Python 标准库 GUI**

![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-0078D6?logo=windows11&logoColor=white)
![UI](https://img.shields.io/badge/GUI-tkinter%20%2F%20tkwebui-8CA6DB)
![Env](https://img.shields.io/badge/%E7%8E%AF%E5%A2%83%E7%AE%A1%E7%90%86-uv-DE5FE9)
![License](https://img.shields.io/badge/License-MIT-green)

</div>

---
## 作者： Jason.Fan 13515358798 微信同号
## 📖 项目简介

这是一个跑在**系统托盘**里的传感器实时监控小工具：后台以可调频率采集传感器数据，绘制成 Web 风格的实时动态曲线，平时隐藏在托盘区不占任务栏，监听运行时托盘图标波形滚动、一眼可辨工作状态。

- **界面层**基于 [tkwebui](../tkwebui)（纯标准库实现的 Web 风格 tkinter 控件库），零第三方 UI 依赖，无需打包浏览器或 Electron；
- **设备层**内置 9 种工业协议，从串口、Modbus 到 MQTT、OPC UA，配置对话框按协议自动生成表单，不改一行图表代码即可接入真实硬件；
- **线程模型**严格分离：采集在后台线程、绘制在主线程，经线程安全队列单向通信，高频采集不卡界面。

## ✨ 功能特性

### 📈 实时曲线

- 多通道动态折线图，每通道独立颜色，底部实时读数；
- 刷新间隔（100–2000 ms）与采集频率（1–100 Hz）独立可调，互不干扰；
- 显示窗口 20–200 点可调，每通道环形缓冲 400 点，启动预填 40 点历史（打开即有曲线）；
- 自动缩放开关：每帧自适应坐标范围，或锁定视图观察细节。

### 🔌 9 种协议接入真实设备

串口 / Modbus / TCP / UDP / HTTP / MQTT / OPC UA / CAN / SNMP（详见[协议一览](#-支持的协议)）。
其中 TCP / UDP / HTTP / SNMP 为纯标准库实现；其余协议依赖可选安装，未安装时程序照常运行，连接时才提示补装。

### 🖥️ 系统托盘（Windows）

- **启动即隐藏**到托盘区，不占任务栏；点窗口 × 也是隐藏而非退出；
- **图标即状态**：监听运行时托盘图标波形滚动，停止后恢复静态；
- **左键 / 双击**打开主界面，**右键**弹出原生菜单（打开主界面 / 隐藏到托盘 / 开始·暂停刷新 / 开始·停止监听 / 设置（连接设备…）/ 退出）；
- 关键操作有**托盘气泡**反馈；托盘不可用时自动退化为窗口内 toast；
- 托盘实现用 `ctypes` 调 Win32（`Shell_NotifyIcon`），**零依赖、无需 .ico 文件**（图标由程序实时绘制）；非 Windows 平台自动退化为普通窗口，功能不受影响。

### 🧵 线程安全设计

- 采集线程**绝不触碰 Tk 控件**，只往队列 `push()`；主线程定时器 `poll()` 后统一重绘；
- 托盘线程同理：回调只投递事件，主线程轮询执行；
- 采集线程出错自动重试并把错误经队列上报，同一条错误只提示一次。

### 🛠️ 工程化

- `uv` 管理 Python 3.11 环境，`uv sync` 一键还原（含 `uv.lock` 精确锁定）；
- 依赖 tkwebui 以**可编辑路径依赖**接入，上游控件库改动即时生效；
- 协议实现全部经过本地回环 / 虚拟总线验证（见[相关文档](#-相关文档)）。

## 🏗️ 架构与数据流

```
┌───────────────────────────── 应用层（本仓库） ─────────────────────────────┐
│                                                                           │
│   live_chart_tray.py            live_chart.py               devices/      │
│   ┌──────────────────┐         ┌────────────────────┐   ┌─────────────┐  │
│   │ TrayChartApp      │ 继承    │ LiveChartApp (Tk)  │   │ 9 种 Device │  │
│   │  托盘线程          │────────▶│  after 定时器主循环 │◀──│  open/read/ │  │
│   │  → 事件队列        │         │  缓冲 / 重绘        │   │  close      │  │
│   └──────────────────┘         └─────────┬──────────┘   └──────┬──────┘  │
│                             poll()       │                     │ read()  │
│                                   ┌──────┴───────┐      ┌──────┴──────┐  │
│                                   │  DataSource   │◀────│ CaptureThread│  │
│                                   │ (线程安全队列) │push │ (后台线程)    │  │
│                                   └──────────────┘      └─────────────┘  │
└───────────────────────────────────┬───────────────────────────────────────┘
                                    │ 只调用 set_data()
                                    ▼
┌───────────────────────────── 控件层 tkwebui ──────────────────────────────┐
│  LineChart · ThemedCombobox · ValueSlider · Spreadsheet · notify          │
└───────────────────────────────────────────────────────────────────────────┘
```

**单向数据流**：`采集线程 → push() 队列 → 主线程 poll() → 环形缓冲 → set_data() 重绘`。
采集按 Hz 节流、重绘按毫秒定时器驱动，同一通道一次刷新只取最新值（实时语义）。

## 🔌 支持的协议

| 协议 | 模块 | 链路 | 通道寻址方式 | 依赖 |
|---|---|---|---|---|
| 串口 Serial | `serial_dev.py` | 逐行读取，keyvalue / csv 解析 | 报文字段名 / 列序 | `pyserial` |
| Modbus | `modbus_dev.py` | TCP / RTU，相邻寄存器自动合并整段读 | 寄存器地址 + 类型 | `pymodbus` |
| TCP | `socket_dev.py` | 原始行报文，可先发查询指令 | 报文字段名 | 无（标准库） |
| UDP | `socket_dev.py` | 数据报，支持广播监听 | 报文字段名 | 无（标准库） |
| HTTP | `http_dev.py` | GET / POST 轮询 REST / JSON 接口 | JSON 路径（`a.b.0`） | 无（标准库） |
| MQTT | `mqtt_dev.py` | 订阅主题，缓存最新消息 | 主题 / 字段名 / JSON 路径 | `paho-mqtt` |
| OPC UA | `opcua_dev.py` | 同步客户端按 NodeId 读节点 | NodeId（`ns=2;i=2`） | `asyncua` |
| CAN / CANopen | `can_dev.py` | 收帧按类型解码 | 帧 ID（`0x123:偏移`） | `python-can` |
| SNMP | `snmp_dev.py` | v1 / v2c GET（自带极简 BER 编解码） | OID | 无（标准库） |

> 所有协议在配置对话框里都支持「测试读取」：临时打开设备读一次并关闭，成功 / 失败立即提示，避免盲目连接。

## 🚀 快速开始

### 环境要求

- Windows 10 / 11（托盘功能仅 Windows；其他平台按普通窗口运行）
- [uv](https://docs.astral.sh/uv/)（自动管理 Python 3.11 虚拟环境）
- **同级目录需要 tkwebui 工程**（`pyproject.toml` 以可编辑方式引用 `../tkwebui`）：

```
任意目录/
├─ tkwebui/                  # 上游控件库（python -m venv 之外还要 clone 它）
└─ tkwebui_传感器托盘/        # 本项目
```

> 若目录结构不同，修改 `pyproject.toml` 中 `[tool.uv.sources]` 的 `tkwebui` 路径即可。

### 安装与运行

```powershell
git clone https://github.com/Fanjun7687/TkWebUI_SensorTray.git
cd TkWebUI_SensorTray

uv sync                              # 创建 .venv 并按 uv.lock 精确安装依赖
uv run python live_chart_tray.py     # 托盘版（默认启动即隐藏到托盘）
```

```powershell
# 其它运行方式
uv run python live_chart_tray.py --show          # 启动时同时打开主界面
uv run python live_chart.py                      # 纯主界面版（无托盘）
.\.venv\Scripts\python.exe live_chart_tray.py    # 直接用虚拟环境解释器（离线环境推荐）
```

### 可选：安装设备协议依赖

TCP / UDP / HTTP / SNMP 开箱即用（纯标准库），其余协议按需安装：

```powershell
uv pip install pyserial pymodbus paho-mqtt python-can asyncua
```

未安装时对应协议仍会出现在下拉框中，尝试连接时会提示补装命令。

## 🖥️ 使用指南

### 主界面

| 操作 | 说明 |
|---|---|
| 数据源下拉框 | 内置伪数据 / 外部推送 / 已连接设备之间切换 |
| 「连接设备…」 | 弹出配置对话框：选协议 → 填参数 → 编辑采集通道表 → 测试读取 → 确定 |
| 「启动采集」 | 启动后台采集线程（伪数据演示或设备读取） |
| 刷新间隔滑块 | 100–2000 ms，控制重绘节奏 |
| 窗口点数滑块 | 20–200 点，控制曲线显示的历史长度 |
| 采集频率滑块 | 1–100 Hz，运行中调整、下一轮生效 |
| 自动缩放 | 勾选后每帧自适应坐标范围 |

### 系统托盘

| 操作 | 行为 |
|---|---|
| 启动程序 | 窗口隐藏，托盘区出现动态波形图标 + 气泡提示 |
| 左键 / 双击图标 | 打开并置前主界面 |
| 右键图标 | 原生菜单：打开主界面 / 隐藏到托盘 / 开始·暂停刷新 / 开始·停止监听 / 设置（连接设备…）/ 退出 |
| 点窗口 × | 隐藏到托盘（**不退出**），气泡提示如何找回 |
| 监听运行中 | 托盘图标波形滚动；停止后恢复静态 |
| 「退出」 | 唯一的真退出入口：停采集线程 → 关设备 → 摘托盘图标 |

### 连接真实设备（以 Modbus TCP 为例）

1. 托盘右键 →「设置（连接设备…）」（或主界面工具栏按钮）；
2. 协议选 `Modbus`，模式选 `tcp`，填主机 / 端口 / 单元号；
3. 通道表编辑采集点：`名称 | 颜色 | 字段/地址 | 类型 | 系数 | 偏移`，例如
   `温度 ℃ | #087DFF | 0 | float32 | 0.1 | 0`（寄存器 0 起、float32、显示值 ×0.1）；
4. 点「测试读取」验证连通与取值；
5. 确定后自动切到设备数据源并启动采集线程，托盘图标开始滚动。

## 🧩 扩展开发

### 新增协议（不改界面与图表）

```python
# devices/mybus_dev.py
from .base import Channel, Device, DeviceError, Option, register

@register
class MyBusDevice(Device):
    protocol = 'mybus'
    label = 'MyBus'

    @classmethod
    def option_fields(cls):
        # 配置对话框按此自动生成表单
        return [Option('host', '地址', 'text', default='127.0.0.1')]

    def open(self):  self._link = open_link(self.opt('host'))
    def close(self): self._link.close()
    def read(self):
        # 返回 {通道名: 数值}；系数换算框架已内置到通道定义
        return {c.name: read_point(c.field) * c.scale + c.offset for c in self.channels}
```

然后在 `devices/__init__.py` 里 `from . import mybus_dev` 一次即可——
协议下拉框、参数表单、采集线程、曲线全部自动生效。

### 新增数据源

实现 `channels()` + `poll()` 两个方法，任何来源（文件、数据库、内存队列）都能接入：

```python
class MySource:
    def channels(self): return [{'name': '温度 ℃', 'color': '#087DFF'}]
    def poll(self):     return {'温度 ℃': read_temp()}   # 无数据时返回 None
```

## 📁 项目结构

```
tkwebui_传感器托盘/
├─ live_chart_tray.py        # 托盘版主界面：启动隐藏、托盘菜单、气泡提示（继承 LiveChartApp）
├─ live_chart.py             # 主界面：数据源抽象、采集线程、实时折线图、设备接入
├─ trayicon.py               # 纯标准库 Windows 托盘图标（ctypes + Shell_NotifyIcon）
├─ devices/                  # 可扩展的设备 / 协议层
│  ├─ __init__.py            #   导入即注册内置协议（import 顺序 = 下拉框顺序）
│  ├─ base.py                #   Device / Channel / Option + 注册表
│  ├─ util.py                #   共用工具：报文解析、JSON 路径、地址解析
│  ├─ serial_dev.py          #   串口（pyserial）
│  ├─ modbus_dev.py          #   Modbus TCP / RTU（pymodbus）
│  ├─ socket_dev.py          #   TCP / UDP 原始套接字（标准库）
│  ├─ http_dev.py            #   HTTP / JSON（标准库）
│  ├─ mqtt_dev.py            #   MQTT（paho-mqtt）
│  ├─ opcua_dev.py           #   OPC UA（asyncua）
│  ├─ can_dev.py             #   CAN / CANopen（python-can）
│  ├─ snmp_dev.py            #   SNMP v1/v2c（标准库实现）
│  └─ dialog.py              #   设备配置对话框（协议 + 参数表单 + 通道表）
├─ pyproject.toml            # 项目元数据与依赖（tkwebui 以可编辑路径依赖接入）
├─ uv.lock                   # 依赖精确锁定
└─ .python-version           # 固定 Python 3.11
```

## 📚 相关文档

- **[AGENTS.md](AGENTS.md)** —— 端到端的数据流、模块职责与关键逻辑梳理：三种数据源、主循环与采集线程时序、9 种协议的通道语义与参数、线程安全规则、扩展点与已验证场景。想二次开发前建议先读它。
- 上游控件库 [tkwebui](../tkwebui)：Web 风格 tkinter/ttk 主题与控件（LineChart、Spreadsheet、ResultDialog 等），零第三方依赖。

## 📄 许可说明

上游控件库 tkwebui 采用 [MIT](../tkwebui/LICENSE) 许可；本项目沿用 MIT 许可。
