"""实时动态折线图 + 系统托盘（以 live_chart.py 为基础，纯标准库实现）。

运行：python examples/live_chart_tray.py          # 启动即隐藏到系统托盘（默认）
      python examples/live_chart_tray.py --show   # 启动时同时打开主界面

主界面与 ``live_chart.py`` 完全一致（直接继承 ``LiveChartApp``），只是增加了托盘行为：

* 启动后窗口隐藏，右下角托盘区出现图标；
* 监听（采集）运行时托盘图标波形滚动，停止后恢复静态；
* 左键 / 双击图标 = 打开主界面；右键图标 = 菜单；
* 菜单：打开主界面 / 隐藏到托盘 / 开始·暂停刷新 / 开始·停止监听 / 设置（连接设备…）/ 退出；
* 点窗口的 × = 隐藏到托盘（不退出），真正退出走菜单「退出」。

托盘图标见 ``trayicon.py``：ctypes 调 Win32（Shell_NotifyIcon），纯标准库、
零依赖、无需 .ico 文件（图标由程序绘制）。仅 Windows 支持；
其他平台自动退化为普通窗口，功能不受影响。

线程模型与 live_chart.py 一致：托盘图标在独立线程里跑 Win32 消息循环，
回调**只往队列里放事件**，主线程用 ``after`` 轮询队列后再操作 Tk，
避免跨线程调用 Tkinter（与「采集线程只 push()」是同一个思路）。
"""

from __future__ import annotations

import queue
import sys

import live_chart  # 复用主界面（tkwebui 已通过 uv 路径依赖装进 .venv，无需再修改 sys.path）
import trayicon  # 同目录的系统托盘实现（纯标准库）
from tkwebui import enable_dpi_awareness, notify

POLL_MS = 120  # 主线程轮询托盘事件的间隔（毫秒）


class TrayChartApp(live_chart.LiveChartApp):
    """带系统托盘的实时曲线窗口。界面、数据流与 ``LiveChartApp`` 完全一致。"""

    def __init__(self, source=None, device=None, start_hidden=True):  # start_hidden：启动后是否隐藏到托盘
        super().__init__(source=source, device=device)  # 界面、数据流全部交给 LiveChartApp
        self.title('TkWebUI · 实时曲线（系统托盘）')  # 换个标题与普通版区分
        self.protocol('WM_DELETE_WINDOW', self.hide_to_tray)  # 点 × = 隐藏，不退出
        self._tray = None  # 托盘图标对象（trayicon.TrayIcon）
        self._events = queue.Queue()  # 托盘线程 -> 主线程的事件队列
        self._build_tray()  # 创建托盘图标
        self.after(POLL_MS, self._drain_tray)  # 主线程开始轮询托盘事件
        if start_hidden:  # 默认：启动即隐藏
            self.withdraw()  # 隐藏主窗口（任务栏里也消失）
            self.after(800, lambda: self.notify_tray('已隐藏到系统托盘，右键图标可操作'))  # 稍后用气泡提示

    # ---------- 托盘 ----------
    def _build_tray(self):  # 创建托盘图标并挂到托盘区
        if not trayicon.AVAILABLE:  # 非 Windows：没有托盘
            self.after(600, lambda: self.notify_tray('当前系统不支持托盘，窗口按普通方式运行'))  # 提示后照常显示窗口
            return
        try:
            tray = trayicon.TrayIcon('tkwebui-live', 'TkWebUI 实时曲线（双击打开，右键菜单）')  # 创建托盘图标对象
            tray.show()  # 挂到托盘（阻塞到初始化完成）
        except Exception as exc:  # 托盘创建失败不影响主功能
            self._tray = None
            self.after(600, lambda error=exc: self.notify_tray(f'托盘创建失败：{error}'))  # 稍后提示失败原因
            return
        self._tray = tray  # 保存引用：退出时要 stop()
        self._tray.on_left = self._tray_open  # 以下两个回调在托盘线程里执行
        self._tray.on_right = self._tray_right_click

    # ---- 托盘线程回调：只投递事件，不碰 Tk ----
    def _tray_open(self):  # 左键 / 双击图标（托盘线程回调）
        self._post(self.show_window)  # 只投递“打开窗口”事件

    def _tray_right_click(self, x, y):  # 右键图标（托盘线程回调，x/y 为屏幕坐标）
        self._tray.show_menu(self._menu_items(), x, y)  # 弹原生菜单：阻塞托盘线程直到用户关闭

    def _post(self, action, *args, balloon=False):  # 托盘线程 → 主线程的投递入口
        """托盘线程 -> 主线程：只投递事件，绝不直接触碰 Tk。"""
        self._events.put((action, args, balloon))  # balloon=True 表示执行后用气泡反馈

    def _drain_tray(self):  # 主线程轮询：执行托盘投递过来的操作
        """主线程轮询：执行托盘投递过来的操作（与 tick_once 一样由 after 驱动）。"""
        while True:
            try:
                action, args, balloon = self._events.get_nowait()  # 非阻塞取一个事件
            except queue.Empty:  # 队列空了，本轮结束
                break
            try:
                action(*args)  # 在主线程执行操作（可以安全触碰 Tk）
                if balloon:  # 需要气泡反馈时
                    self.notify_tray(self.status.get())  # 把状态栏文字弹成气泡
            except Exception as exc:  # 单个事件失败不影响其它
                self.status.set(f'托盘操作出错：{type(exc).__name__}: {exc}')
        self.after(POLL_MS, self._drain_tray)  # 排定下一轮轮询

    # ---------- 右键菜单 ----------
    def _menu_items(self):  # 菜单项定义（托盘线程里读取，回调只投递事件）
        """托盘右键菜单项：``(文字, 回调)`` 或分隔线 ``'---'``；回调都在主线程执行。"""
        return [
            ('打开主界面', lambda: self._post(self.show_window)),  # 打开主窗口
            ('隐藏到托盘', lambda: self._post(self.hide_to_tray)),  # 隐藏窗口
            '---',  # 分隔线
            ('暂停刷新' if self.running else '开始刷新', lambda: self._post(self.toggle)),  # 文案随刷新状态变化
            (
                '停止监听' if self.capturing() else '开始监听',
                lambda: self._post(self.toggle_capture, balloon=True),
            ),  # 采集开关
            ('设置（连接设备…）', lambda: self._post(self.open_settings)),  # 打开设备设置对话框
            '---',  # 分隔线
            ('退出', lambda: self._post(self.close)),  # 真正退出程序的唯一入口
        ]

    # ---------- 窗口显隐与操作 ----------
    def show_window(self):  # 从托盘恢复主界面
        """从托盘恢复主界面并置前。"""
        self.deiconify()  # 恢复显示（withdraw 的逆操作）
        self.lift()  # 提到最前
        self.focus_force()  # 抢焦点
        self.attributes('-topmost', True)  # 临时置顶，确保被看到
        self.after(200, lambda: self.attributes('-topmost', False))  # 0.2 秒后取消置顶

    def hide_to_tray(self):  # 隐藏窗口并留在托盘（点 × 的行为）
        """隐藏窗口并留在托盘（点 × 的行为）。"""
        self.withdraw()  # 隐藏窗口，托盘图标仍在
        self.notify_tray('已隐藏到系统托盘；右键图标可选择打开 / 监听 / 退出')  # 气泡提示

    def open_settings(self):  # 托盘菜单里的“设置”
        """先显示窗口再弹设置对话框，避免对话框依附在隐藏窗口上。"""
        self.show_window()  # 先恢复窗口：对话框要依附在可见窗口上
        self.open_device_dialog()  # 再弹出设备设置对话框

    def capturing(self):  # 采集线程是否在运行
        """采集线程是否在运行（托盘菜单文案会用到）。"""
        return self.capture is not None and self.capture.is_alive()  # 线程还活着才算在监听

    # ---- 采集启停：挂钩托盘动画（图标滚动 = 正在监听）----
    def start_capture(self):  # 启动采集：托盘图标开始滚动波形
        super().start_capture()
        if self._tray is not None:
            self._tray.start_animation()  # 图标动起来 = 正在监听

    def stop_capture(self):  # 停止采集：托盘图标恢复静态
        super().stop_capture()
        if self._tray is not None:
            self._tray.stop_animation()

    def connect_device(self, device):  # 连接真实设备：成功后托盘图标开始滚动
        super().connect_device(device)
        if self._tray is not None and self.device is device:  # super() 成功时会把 self.device 设为该设备
            self._tray.start_animation()

    def notify_tray(self, message, title='TkWebUI 实时曲线'):  # 提示：优先托盘气泡，退化为窗口 toast
        """托盘气泡提示；托盘不可用时退化为窗口内 toast。"""
        if self._tray is not None and self._tray.notify(message, title):  # 托盘气泡弹成功
            return
        try:
            notify(self, message, kind='info')  # 退化为窗口内 toast
        except Exception:  # 窗口隐藏时 toast 可能不可见，忽略
            pass

    # ---------- 退出 ----------
    def close(self):  # 真正退出程序
        """真正退出：先移除托盘图标，再走 LiveChartApp 的清理流程。"""
        if self._tray is not None:
            try:
                self._tray.stop()  # 摘掉托盘图标、结束托盘线程
            except Exception:  # 停不掉也不阻塞退出
                pass
            self._tray = None
        super().close()  # 走 LiveChartApp 的清理：停采集线程 / 关设备 / 销毁窗口


def main(source=None, device=None):  # 启动示例
    """启动示例。加 ``--show`` 参数可让主界面随程序一起打开。"""
    enable_dpi_awareness()  # 必须在创建 Tk() 之前调用
    start_hidden = '--show' not in sys.argv  # 命令行带 --show 时启动即显示窗口
    TrayChartApp(source=source, device=device, start_hidden=start_hidden).mainloop()  # 进入事件循环


if __name__ == '__main__':  # 直接运行本文件时才启动
    main()
