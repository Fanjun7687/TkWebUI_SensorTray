"""Windows 系统托盘图标（纯标准库 ctypes 实现，零依赖）。

提供 ``TrayIcon``：在托盘区显示一个**程序内绘制**的图标（不需要 .ico 文件、
不需要 Pillow），左键 / 双击与右键分别回调，支持气泡提示；任务栏重建
（explorer 重启）后自动补挂图标。

用法::

    icon = TrayIcon('myapp', '提示文本')
    icon.on_left = lambda: print('打开')            # 托盘线程回调
    icon.on_right = lambda x, y: print('右键', x, y)  # 屏幕坐标
    icon.show()
    icon.notify('已隐藏', '提示')
    icon.show_menu([('打开', open_cb), '---', ('退出', quit_cb)])  # 原生右键菜单
    icon.stop()

注意：``on_left`` / ``on_right`` 在托盘线程里执行，**不要直接操作 Tk**；
把事件投递回主线程（``queue.Queue`` + ``after`` 轮询），示例见 live_chart_tray.py。

实现要点：``Shell_NotifyIconW`` 挂图标；一个隐藏窗口接收鼠标回调消息；
GDI 的 ``CreateDIBSection`` + ``CreateIconIndirect`` 把像素数组变成 HICON。

右键菜单：``show_menu`` 用原生 Win32 菜单（``TrackPopupMenu``）在托盘线程里弹出；
弹出前必须把宿主窗口设为前台（``SetForegroundWindow``），否则点击菜单外它不会关闭。

动画：``start_animation(fps)`` / ``stop_animation()`` 让波形滚动——
预生成多帧不同相位的图标，托盘线程里用 ``SetTimer`` 定时 ``NIM_MODIFY`` 换帧。
"""

from __future__ import annotations  # 允许注解里使用新语法（Python 3.11 下保持习惯一致）

import math  # 画折线用：math.sin / math.hypot
import sys  # 判断操作系统
import threading  # 托盘消息循环线程与同步事件

AVAILABLE = sys.platform == 'win32'  # 仅 Windows 支持（非 Windows 时本模块提供占位类）

if not AVAILABLE:  # 非 Windows：占位实现，保证 import 不报错

    class TrayIcon:  # 占位类：非 Windows 平台
        def __init__(self, *args, **kwargs):  # 一实例化就报错提示
            raise OSError('系统托盘仅支持 Windows')

else:  # ---- Windows 实现 ----
    import ctypes  # 调用 Win32 API
    from ctypes import wintypes  # Windows 基本类型（HWND/DWORD/...）

    user32 = ctypes.WinDLL('user32', use_last_error=True)  # 窗口与消息相关 API
    gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)  # 绘图 API（画图标位图）
    shell32 = ctypes.WinDLL('shell32', use_last_error=True)  # 托盘 API（Shell_NotifyIcon）
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)  # 模块句柄等

    LRESULT = ctypes.c_ssize_t  # 消息处理函数的返回值（指针大小，x64 上是 8 字节）
    WNDPROC = ctypes.WINFUNCTYPE(
        LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )  # 窗口过程回调类型

    WM_NULL = 0x0000  # 空消息：弹出菜单后发送，重置前台状态（微软 KB 建议）
    WM_CLOSE = 0x0010  # 关闭窗口消息：stop() 用它通知托盘线程退出
    WM_TIMER = 0x0113  # 定时器消息：动画每帧触发一次
    WM_DESTROY = 0x0002  # 窗口已销毁：发 PostQuitMessage 结束消息循环
    WM_LBUTTONUP = 0x0202  # 左键抬起（单击）
    WM_LBUTTONDBLCLK = 0x0203  # 左键双击
    WM_RBUTTONUP = 0x0205  # 右键抬起：弹出菜单
    WM_APP_TRAY = 0x8001  # 托盘回调消息（WM_APP 区间）
    WM_APP_ANIM = 0x8002  # 自定义消息：通知托盘线程开始/停止动画（wparam=1 开 / 0 停）
    ANIM_TIMER_ID = 1  # 动画定时器 ID（SetTimer 用）
    ANIM_FRAMES = 12  # 动画帧数：一个波形周期均分成 12 帧

    NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2  # Shell_NotifyIcon 的三种操作：添加/修改/删除
    NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10  # 本次调用生效的字段：回调消息/图标/悬浮提示/气泡
    NIIF_INFO = 0x01  # 气泡带“信息”图标
    DIB_RGB_COLORS = 0  # 位图颜色表类型：直接 RGB
    MF_STRING, MF_SEPARATOR = 0x0000, 0x0800  # 菜单项类型：普通文本 / 分隔线
    TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100  # 菜单标志：支持右键 / TrackPopupMenu 返回选中项 ID

    class ICONINFO(ctypes.Structure):  # CreateIconIndirect 的输入：两张位图合成一个图标
        _fields_ = [
            ('fIcon', wintypes.BOOL),  # TRUE = 图标（而不是光标）
            ('xHotspot', wintypes.DWORD),  # 热点 X（图标用不到）
            ('yHotspot', wintypes.DWORD),  # 热点 Y（图标用不到）
            ('hbmMask', wintypes.HBITMAP),  # 单色掩码位图（全 0 = 不透明）
            ('hbmColor', wintypes.HBITMAP),  # 彩色位图（32 位含 alpha 通道）
        ]

    class BITMAPINFOHEADER(ctypes.Structure):  # GDI 位图信息头
        _fields_ = [
            ('biSize', wintypes.DWORD),  # 本结构大小
            ('biWidth', wintypes.LONG),  # 宽（像素）
            ('biHeight', wintypes.LONG),  # 高；负数表示自上而下
            ('biPlanes', wintypes.WORD),  # 恒为 1
            ('biBitCount', wintypes.WORD),  # 每像素位数（32 = BGRA）
            ('biCompression', wintypes.DWORD),  # 压缩方式（0 = 不压缩）
            ('biSizeImage', wintypes.DWORD),  # 图像字节数（可填 0）
            ('biXPelsPerMeter', wintypes.LONG),  # 水平分辨率（不用）
            ('biYPelsPerMeter', wintypes.LONG),  # 垂直分辨率（不用）
            ('biClrUsed', wintypes.DWORD),  # 使用的颜色数（0 = 全部）
            ('biClrImportant', wintypes.DWORD),  # 重要颜色数（0 = 全部）
        ]

    class NOTIFYICONDATAW(ctypes.Structure):  # Shell_NotifyIcon 的参数结构（x64 共 976 字节）
        _fields_ = [
            ('cbSize', wintypes.DWORD),  # 本结构大小（必填）
            ('hWnd', wintypes.HWND),  # 接收托盘鼠标回调消息的窗口
            ('uID', wintypes.UINT),  # 图标 ID（同一窗口可挂多个图标）
            ('uFlags', wintypes.UINT),  # 哪些字段生效（NIF_*）
            ('uCallbackMessage', wintypes.UINT),  # 鼠标事件回调消息编号
            ('hIcon', wintypes.HICON),  # 图标句柄
            ('szTip', wintypes.WCHAR * 128),  # 鼠标悬浮提示文字
            ('dwState', wintypes.DWORD),  # 图标状态（不用）
            ('dwStateMask', wintypes.DWORD),  # 状态掩码（不用）
            ('szInfo', wintypes.WCHAR * 256),  # 气泡正文
            ('uVersion', wintypes.UINT),  # 版本号/超时（共用内存，这里不用）
            ('szInfoTitle', wintypes.WCHAR * 64),  # 气泡标题
            ('dwInfoFlags', wintypes.DWORD),  # 气泡图标类型（NIIF_*）
            ('guidItem', ctypes.c_ubyte * 16),  # 图标 GUID（不用）
            ('hBalloonIcon', wintypes.HICON),  # 气泡自定义图标（不用）
        ]

    class WNDCLASSEXW(ctypes.Structure):  # 窗口类：注册后才能创建窗口
        _fields_ = [
            ('cbSize', wintypes.UINT),  # 本结构大小
            ('style', wintypes.UINT),  # 类样式（0 即可）
            ('lpfnWndProc', WNDPROC),  # 窗口过程：系统把消息派发到这里
            ('cbClsExtra', ctypes.c_int),  # 类附加内存（0）
            ('cbWndExtra', ctypes.c_int),  # 窗口附加内存（0）
            ('hInstance', wintypes.HINSTANCE),  # 实例句柄
            ('hIcon', wintypes.HICON),  # 窗口图标（隐藏窗口不用）
            ('hCursor', wintypes.HANDLE),  # 光标（None = 默认）
            ('hbrBackground', wintypes.HBRUSH),  # 背景刷（窗口不显示，不用）
            ('lpszMenuName', wintypes.LPCWSTR),  # 菜单名（无）
            ('lpszClassName', wintypes.LPCWSTR),  # 类名（每个实例唯一）
            ('hIconSm', wintypes.HICON),  # 小图标（不用）
        ]

    # 函数原型：在 x64 上必须声明指针大小的返回值，否则句柄会被截断成 32 位
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE  # 取当前 exe 的模块句柄
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]  # 参数：模块名（None = 当前 exe）
    user32.RegisterClassExW.restype = ctypes.c_ushort  # 注册窗口类，返回原子值
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]  # 参数：窗口类结构指针
    user32.UnregisterClassW.restype = wintypes.BOOL  # 注销窗口类
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]  # 参数：类名、实例句柄
    user32.CreateWindowExW.restype = wintypes.HWND  # 创建（永不显示的）消息窗口
    user32.CreateWindowExW.argtypes = [  # 12 个参数，见下面逐行说明
        wintypes.DWORD,  # 扩展样式
        wintypes.LPCWSTR,  # 类名
        wintypes.LPCWSTR,  # 窗口标题
        wintypes.DWORD,  # 样式（0 = 不显示）
        ctypes.c_int,  # x
        ctypes.c_int,  # y
        ctypes.c_int,  # 宽
        ctypes.c_int,  # 高
        wintypes.HWND,  # 父窗口
        wintypes.HMENU,  # 菜单
        wintypes.HINSTANCE,  # 实例句柄
        wintypes.LPVOID,  # 附加参数
    ]
    user32.DefWindowProcW.restype = LRESULT  # 默认消息处理：没处理的消息交给系统
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]  # 窗口/消息/两个参数
    user32.DestroyWindow.restype = wintypes.BOOL  # 销毁窗口
    user32.DestroyWindow.argtypes = [wintypes.HWND]  # 参数：窗口句柄
    user32.PostQuitMessage.restype = None  # 结束消息循环（GetMessage 返回 0）
    user32.PostQuitMessage.argtypes = [ctypes.c_int]  # 参数：退出码
    user32.GetMessageW.restype = ctypes.c_int  # 取消息：>0 继续，=0 退出循环，-1 出错
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    ]  # 消息/窗口/过滤
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]  # 翻译键盘消息（惯例保留）
    user32.DispatchMessageW.restype = LRESULT  # 把消息派发给窗口过程
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]  # 参数：消息结构
    user32.PostMessageW.restype = wintypes.BOOL  # 向窗口投递消息（stop() 发 WM_CLOSE、动画启停都用它）
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]  # 窗口/消息/参数
    user32.SetTimer.restype = ctypes.c_size_t  # 安装定时器：定期向窗口投 WM_TIMER（返回定时器 ID）
    user32.SetTimer.argtypes = [
        wintypes.HWND,
        ctypes.c_size_t,
        wintypes.UINT,
        ctypes.c_void_p,
    ]  # 窗口/ID/间隔ms/回调(None=走窗口过程)
    user32.KillTimer.restype = wintypes.BOOL  # 移除定时器
    user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]  # 窗口/ID
    user32.SetForegroundWindow.restype = wintypes.BOOL  # 把窗口设为前台（菜单能被点击外部关闭的前提）
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]  # 参数：窗口句柄
    user32.CreatePopupMenu.restype = wintypes.HMENU  # 创建原生弹出菜单
    user32.CreatePopupMenu.argtypes = []
    user32.AppendMenuW.restype = wintypes.BOOL  # 向菜单添加菜单项
    user32.AppendMenuW.argtypes = [
        wintypes.HMENU,
        wintypes.UINT,
        ctypes.c_size_t,
        wintypes.LPCWSTR,
    ]  # 菜单/类型/ID/文字
    user32.TrackPopupMenu.restype = ctypes.c_int  # 弹出菜单并阻塞（TPM_RETURNCMD 时返回选中项 ID）
    user32.TrackPopupMenu.argtypes = [  # 菜单/标志/x/y/保留/宿主窗口/排除矩形(None)
        wintypes.HMENU,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        ctypes.c_void_p,
    ]
    user32.DestroyMenu.restype = wintypes.BOOL  # 销毁菜单句柄
    user32.DestroyMenu.argtypes = [wintypes.HMENU]  # 参数：菜单句柄
    user32.GetCursorPos.restype = wintypes.BOOL  # 取鼠标当前位置（右键菜单弹在这里）
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]  # 参数：POINT 输出
    user32.RegisterWindowMessageW.restype = wintypes.UINT  # 注册系统广播消息号（TaskbarCreated）
    user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]  # 参数：消息名
    user32.DestroyIcon.restype = wintypes.BOOL  # 销毁图标句柄
    user32.DestroyIcon.argtypes = [wintypes.HICON]  # 参数：图标句柄
    user32.CreateIconIndirect.restype = wintypes.HICON  # 由两张位图合成图标
    user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]  # 参数：ICONINFO 指针
    user32.GetDC.restype = wintypes.HDC  # 取设备上下文（建 DIB 位图用）
    user32.GetDC.argtypes = [wintypes.HWND]  # 参数：窗口（None = 整个屏幕）
    user32.ReleaseDC.restype = ctypes.c_int  # 释放设备上下文
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]  # 参数：窗口、DC
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP  # 创建可直接读写像素的位图
    gdi32.CreateDIBSection.argtypes = [  # 参数见下
        wintypes.HDC,  # 设备上下文
        ctypes.c_void_p,  # BITMAPINFO*
        wintypes.UINT,  # 颜色类型
        ctypes.POINTER(ctypes.c_void_p),  # 输出：像素内存地址
        wintypes.HANDLE,  # 文件映射对象（不用）
        wintypes.DWORD,  # 位偏移（不用）
    ]
    gdi32.CreateBitmap.restype = wintypes.HBITMAP  # 创建单色掩码位图
    gdi32.CreateBitmap.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
    ]  # 宽/高/平面数/位数/像素数据
    gdi32.DeleteObject.restype = wintypes.BOOL  # 删除 GDI 对象（位图）
    gdi32.DeleteObject.argtypes = [wintypes.HANDLE]  # 参数：GDI 句柄
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL  # 托盘核心 API：添加/修改/删除图标
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]  # 操作码、参数结构

    # ---------------- 图标绘制：像素数组 -> HICON ----------------

    def _distance(px, py, points):  # 计算点到折线的最短距离
        """点到折线的最短距离（用于画白色曲线）。"""
        best = 1e9  # 初始“最短距离”设为极大值
        for (x1, y1), (x2, y2) in zip(points, points[1:]):  # 遍历折线的每条线段
            dx, dy = x2 - x1, y2 - y1  # 线段方向向量
            length = dx * dx + dy * dy  # 线段长度的平方
            if length == 0:  # 两端重合：没有方向
                ratio = 0.0
            else:
                ratio = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length))  # 投影比例，夹到 [0,1]
            ex, ey = x1 + ratio * dx - px, y1 + ratio * dy - py  # 线段最近点 → 该像素 的向量
            best = min(best, math.hypot(ex, ey))  # 更新最短距离
        return best

    def _icon_pixels(size=32, phase=0.0):  # 生成图标的 32 位 BGRA 像素数组（phase 控制波形相位）
        """生成托盘图标的 32 位 BGRA 像素：蓝色圆盘 + 白色迷你折线。"""
        blue = (0xFF, 0x7D, 0x08, 0xFF)  # #087DFF -> BGRA
        white = (0xFF, 0xFF, 0xFF, 0xFF)  # 白色折线
        center = (size - 1) / 2.0  # 圆心坐标
        radius = size / 2.0 - 1  # 圆盘半径（留 1px 边）
        inset = max(3, size // 8)  # 折线距边缘的留白
        half = max(1.5, size / 14.0)  # 折线线宽的一半
        points = []  # 折线顶点集合
        for index in range(7):  # 7 个顶点连成一条迷你曲线
            x = inset + (size - 2 * inset) * index / 6  # 横向均分
            wave = 0.5 + 0.5 * math.sin(index * 1.05 + phase)  # 0~1 的正弦波形；phase 递增 = 波形向左流动
            y = size - inset - (size - 2 * inset) * (0.18 + 0.62 * wave)  # 纵向映射到 18%~80% 高度
            points.append((x, y))
        rows = bytearray()  # 逐像素输出
        for py in range(size):  # 每一行
            for px in range(size):  # 每一列
                dx, dy = px + 0.5 - center, py + 0.5 - center  # 像素中心相对圆心的偏移
                color = (0, 0, 0, 0)  # 默认全透明
                if dx * dx + dy * dy <= radius * radius:  # 落在圆盘内
                    color = white if _distance(px + 0.5, py + 0.5, points) <= half else blue  # 折线用白色，其余蓝色
                rows += bytes(color)  # 追加 BGRA 四个字节
        return bytes(rows)

    def _make_icon(size=32, phase=0.0):  # 像素数组 → HICON 图标句柄（phase 不同 = 动画的不同帧）
        """把像素数组变成 HICON（GDI 位图 + CreateIconIndirect）。"""
        bits = _icon_pixels(size, phase)  # 先画好像素数据
        header = BITMAPINFOHEADER()  # 位图信息头
        header.biSize = ctypes.sizeof(header)  # 结构大小
        header.biWidth = size  # 宽
        header.biHeight = -size  # 负数 = 自上而下
        header.biPlanes = 1  # 恒为 1
        header.biBitCount = 32  # 每像素 32 位（BGRA）
        header.biCompression = DIB_RGB_COLORS  # 不压缩
        hdc = user32.GetDC(None)  # 取屏幕 DC
        pixels = ctypes.c_void_p()  # 接收像素内存地址
        color = gdi32.CreateDIBSection(
            hdc, ctypes.byref(header), DIB_RGB_COLORS, ctypes.byref(pixels), None, 0
        )  # 创建可写位图
        user32.ReleaseDC(None, hdc)  # 用完立刻释放
        if not color or not pixels:  # 创建失败
            raise OSError('CreateDIBSection 失败')
        ctypes.memmove(pixels, bits, len(bits))  # 把像素数据拷进位图
        row = ((size + 31) // 32) * 4  # 掩码每行按 4 字节对齐
        mask = gdi32.CreateBitmap(size, size, 1, 1, (ctypes.c_ubyte * (row * size))())  # 全 0 掩码 = 不透明
        if not mask:  # 创建失败
            gdi32.DeleteObject(color)  # 别漏了已建好的彩色位图
            raise OSError('CreateBitmap 失败')
        info = ICONINFO(True, 0, 0, mask, color)  # fIcon=True：这是图标不是光标
        hicon = user32.CreateIconIndirect(ctypes.byref(info))  # 两张位图合成 HICON
        gdi32.DeleteObject(mask)  # 图标内部已复制位图，原件可以删掉
        gdi32.DeleteObject(color)
        if not hicon:  # 合成失败
            raise OSError('CreateIconIndirect 失败')
        return hicon

    # ---------------- 托盘图标 ----------------

    class TrayIcon:
        """Windows 托盘图标。``show()`` 后在独立线程里跑 Win32 消息循环。

        回调 ``on_left()`` / ``on_right(x, y)`` 在**托盘线程**里执行，
        不要直接操作 Tk；把事件投递回主线程即可（见 live_chart_tray.py）。
        """

        def __init__(self, name='tkwebui', tip='', size=32):  # name 标识；tip 悬浮提示；size 图标边长（像素）
            self.name = name  # 程序内标识（不参与系统去重）
            self.tip = tip  # 鼠标悬浮提示文字
            self.size = size  # 图标像素尺寸
            self.on_left = None  # 左键 / 双击
            self.on_right = None  # 右键，参数为屏幕坐标 (x, y)
            self._uid = 0  # 图标 ID（本程序只挂一个）
            self._thread = None  # 托盘线程
            self._hwnd = None  # 消息窗口句柄
            self._hicon = None  # 图标句柄
            self._wndproc = None  # 窗口过程引用（防止被垃圾回收）
            self._hinstance = None  # 模块句柄
            self._class_name = f'TkwebuiTray_{id(self):x}'  # 唯一窗口类名（避免与其他实例冲突）
            self._added = False  # 图标是否已挂到托盘
            self._frames = []  # 动画各帧的 HICON（frames[0] 是静态图标）
            self._frame_index = 0  # 当前帧序号
            self._anim_timer = 0  # 动画定时器 ID（0 = 未在动画）
            self._ready = threading.Event()  # 线程初始化完成信号
            self._error = None  # 初始化失败的错误信息
            self._taskbar_created = user32.RegisterWindowMessageW('TaskbarCreated')  # 任务栏重建的广播消息号

        # ---- 生命周期 ----
        def show(self):  # 在托盘显示图标（阻塞到初始化完成）
            """在托盘区显示图标；创建失败抛 ``OSError``。"""
            if self._thread is not None:  # 已经在运行
                return
            self._thread = threading.Thread(target=self._run, name='tkwebui-tray', daemon=True)  # 守护线程跑消息循环
            self._thread.start()
            if not self._ready.wait(3.0):  # 最多等 3 秒
                raise OSError('托盘图标初始化超时')
            if self._error:  # 线程里创建失败
                raise OSError(f'创建托盘图标失败：{self._error}')

        def stop(self):  # 移除图标并结束托盘线程
            """移除图标并结束托盘线程。"""
            if self._hwnd:  # 窗口还在
                user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)  # 投递 WM_CLOSE，线程自行清理后退出

        def set_tip(self, text):  # 修改鼠标悬浮提示
            """修改鼠标悬浮提示。"""
            self.tip = text  # 先记下来（图标重建时要用）
            if self._added:  # 已挂上才需要更新
                try:
                    self._shell(NIM_MODIFY, self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP))  # NIM_MODIFY 更新提示
                except OSError:  # 失败静默（例如 explorer 正在重启）
                    pass

        def notify(self, message, title=''):  # 气泡提示
            """托盘气泡提示。"""
            if not self._added:  # 图标没挂上没法弹气泡
                return False
            data = self._data(NIF_INFO)  # 本次只更新气泡相关字段
            data.szInfo = str(message)[:255]  # 气泡正文（超长截断）
            data.szInfoTitle = str(title or '')[:63]  # 气泡标题
            data.dwInfoFlags = NIIF_INFO  # 带“信息”小图标
            try:
                self._shell(NIM_MODIFY, data)  # 提交修改
                return True
            except OSError:  # 弹失败不算致命
                return False

        # ---- 动画：滚动波形 ----
        def start_animation(self, fps=8):  # 开始波形滚动动画（可从任意线程调用）
            """让托盘图标滚动波形；``fps`` 是每秒帧数（建议 6~12）。"""
            if self._hwnd:  # 托盘线程已就绪才投递
                user32.PostMessageW(self._hwnd, WM_APP_ANIM, 1, int(fps))  # 具体工作在托盘线程里做

        def stop_animation(self):  # 停止动画，恢复静态图标（可从任意线程调用）
            """停止波形滚动，恢复静态图标。"""
            if self._hwnd:
                user32.PostMessageW(self._hwnd, WM_APP_ANIM, 0, 0)

        # ---- 右键菜单：原生 Win32 菜单 ----
        def show_menu(self, items, x=None, y=None):  # 弹出原生右键菜单（阻塞到用户关闭）
            """弹出原生右键菜单（在托盘线程里执行，阻塞到菜单关闭）。

            ``items``：菜单项列表，元素为 ``(文字, 回调)`` 或分隔线 ``'---'``；
            回调在托盘线程里调用，**不要直接操作 Tk**（用队列投递回主线程）。
            """
            if not self._hwnd:  # 托盘线程未就绪
                return
            if x is None or y is None:  # 未指定位置：弹在鼠标当前处
                point = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(point))
                x, y = point.x, point.y
            menu = user32.CreatePopupMenu()  # 创建原生弹出菜单
            if not menu:
                return
            actions = []  # (菜单项 ID, 回调)
            chosen = 0  # 选中的菜单项 ID（0 = 用户点击别处 / 按 Esc 关闭）
            try:
                item_id = 1  # 菜单项 ID 从 1 开始
                for item in items:
                    if item == '---':  # 分隔线
                        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                    else:
                        text, callback = item
                        user32.AppendMenuW(menu, MF_STRING, item_id, str(text))
                        actions.append((item_id, callback))
                        item_id += 1
                user32.SetForegroundWindow(self._hwnd)  # 关键修复：宿主在前台，点击菜单外才会关闭（KB135788）
                chosen = user32.TrackPopupMenu(  # 阻塞，直到选中某项或点击别处关闭
                    menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, int(x), int(y), 0, self._hwnd, None
                )
                user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)  # 重置前台状态，保证下次菜单也能正常关闭
            finally:
                user32.DestroyMenu(menu)  # 菜单用完即销毁（每次重建，保证文案最新）
            for chosen_id, callback in actions:  # 用户选中了某项：执行回调（托盘线程里）
                if chosen == chosen_id:
                    self._safe(callback)  # 异常不外泄
                    break

        # ---- 内部实现 ----
        def _data(self, flags):  # 组装 NOTIFYICONDATAW 参数结构
            data = NOTIFYICONDATAW()
            data.cbSize = ctypes.sizeof(data)  # 必填：结构大小
            data.hWnd = self._hwnd  # 回调消息发到这个窗口
            data.uID = self._uid  # 图标 ID
            data.uFlags = flags  # 本次调用生效的字段
            data.uCallbackMessage = WM_APP_TRAY  # 鼠标事件发到这个消息号
            data.hIcon = self._hicon  # 图标句柄
            data.szTip = (self.tip or '')[:127]  # 悬浮提示（含结尾 \0 最多 128 字符）
            return data

        def _shell(self, action, data):  # 调 Shell_NotifyIcon，失败抛错
            if not shell32.Shell_NotifyIconW(action, ctypes.byref(data)):  # 返回 0 = 失败
                raise ctypes.WinError(ctypes.get_last_error())  # 带上系统错误码

        def _run(self):  # 托盘线程主体：建图标 + 窗口，然后跑消息循环
            try:
                self._hinstance = kernel32.GetModuleHandleW(None)  # 当前 exe 的模块句柄
                self._hicon = _make_icon(self.size)  # 程序内画一个图标（无需 .ico 文件）
                self._wndproc = WNDPROC(self._on_message)  # 保存引用，防止被回收
                window_class = WNDCLASSEXW()  # 注册窗口类
                window_class.cbSize = ctypes.sizeof(window_class)  # 结构大小
                window_class.lpfnWndProc = self._wndproc  # 消息派发到我们的处理函数
                window_class.hInstance = self._hinstance  # 实例句柄
                window_class.lpszClassName = self._class_name  # 唯一类名
                if not user32.RegisterClassExW(ctypes.byref(window_class)):  # 注册失败
                    raise ctypes.WinError(ctypes.get_last_error())
                self._hwnd = user32.CreateWindowExW(  # 创建隐藏窗口：只用来接收托盘消息
                    0, self._class_name, 'tkwebui-tray', 0, 0, 0, 0, 0, None, None, self._hinstance, None
                )
                if not self._hwnd:  # 创建失败
                    raise ctypes.WinError(ctypes.get_last_error())
                self._shell(NIM_ADD, self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP))  # 把图标挂上托盘
                self._added = True  # 标记：图标已挂上
            except Exception as exc:  # 创建失败：把错误交给 show()
                self._error = str(exc)
                self._ready.set()  # 唤醒 show()
                return
            self._ready.set()  # 初始化完成
            message = wintypes.MSG()  # 消息循环：收消息 → 派发
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:  # 返回 0 时退出循环
                user32.TranslateMessage(ctypes.byref(message))  # 翻译键盘消息（惯例保留）
                user32.DispatchMessageW(ctypes.byref(message))  # 派发到 _on_message
            user32.UnregisterClassW(self._class_name, self._hinstance)  # 循环结束：注销窗口类

        def _on_message(self, hwnd, message, wparam, lparam):  # 窗口过程：托盘的所有事件都从这里进
            if message == WM_APP_TRAY:  # 托盘鼠标事件
                self._dispatch_mouse(lparam & 0xFFFF)  # lParam 低 16 位是具体事件
                return 0
            if message == WM_APP_ANIM:  # 主线程发来的动画启停请求
                if wparam:  # wparam=1：开始
                    self._start_timer(max(1, int(lparam) or 8))
                else:  # wparam=0：停止
                    self._stop_timer()
                return 0
            if message == WM_TIMER and wparam == ANIM_TIMER_ID:  # 动画节拍
                self._advance_frame()  # 换下一帧
                return 0
            if message == WM_CLOSE:  # stop() 发来的退出请求
                self._remove()  # 先摘图标、销毁图标句柄
                user32.DestroyWindow(hwnd)  # 销毁消息窗口
                return 0
            if message == WM_DESTROY:  # 窗口已销毁
                user32.PostQuitMessage(0)  # 让 GetMessage 返回 0，结束循环
                return 0
            if message == self._taskbar_created and self._added:  # explorer 重启后补挂
                self._added = False  # 先当作没挂上
                try:
                    self._shell(NIM_ADD, self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP))  # 重新添加图标
                    self._added = True
                except OSError:  # 补挂失败就等下一次广播
                    pass
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)  # 其余消息交给系统默认处理

        def _dispatch_mouse(self, event):  # 把鼠标事件分派给对应回调
            if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK) and self.on_left:  # 单击或双击左键
                self._safe(self.on_left)  # 调回调（异常不外泄）
            elif event == WM_RBUTTONUP and self.on_right:  # 抬起右键
                point = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(point))  # 取鼠标位置：菜单弹在这里
                self._safe(self.on_right, point.x, point.y)  # 回传屏幕坐标

        def _safe(self, callback, *args):  # 调回调并吞掉异常
            try:
                callback(*args)
            except Exception:  # 托盘线程里的异常不能让进程崩溃
                pass

        # ---- 动画内部：定时器与换帧（都在托盘线程里执行）----
        def _start_timer(self, fps):  # 预生成各帧图标并启动定时器
            if self._anim_timer or not self._hicon:  # 已在动画 / 没有基础图标
                return
            if len(self._frames) < 2:  # 还没生成过帧：frames[0] 复用静态图标
                try:
                    step = math.tau / ANIM_FRAMES  # 一个波形周期均分成 ANIM_FRAMES 份
                    self._frames = [self._hicon]  # 第 0 帧 = 现有静态图标（phase=0）
                    self._frames += [_make_icon(self.size, index * step) for index in range(1, ANIM_FRAMES)]
                except Exception:  # 生成失败：保持静态图标
                    self._frames = [self._hicon]
                    return
            self._frame_index = 0  # 从第 0 帧开始
            self._anim_timer = user32.SetTimer(
                self._hwnd, ANIM_TIMER_ID, max(30, int(1000 / fps)), None
            )  # 定时投 WM_TIMER

        def _advance_frame(self):  # 换下一帧（WM_TIMER 触发）
            if len(self._frames) < 2:  # 没有帧可换
                return
            self._frame_index = (self._frame_index + 1) % len(self._frames)  # 循环推进
            self._hicon = self._frames[self._frame_index]  # 记住当前帧（补挂图标时用它）
            try:
                self._shell(NIM_MODIFY, self._data(NIF_ICON))  # 只更新图标，托盘立即重绘
            except OSError:  # 换帧失败下一帧再试
                pass

        def _stop_timer(self):  # 停定时器，恢复静态并释放多余帧
            if not self._anim_timer:  # 本来就没在动画
                return
            user32.KillTimer(self._hwnd, self._anim_timer)
            self._anim_timer = 0
            self._frame_index = 0
            if len(self._frames) > 1:  # 回到第 0 帧（静态图标）
                self._hicon = self._frames[0]
                try:
                    self._shell(NIM_MODIFY, self._data(NIF_ICON))  # 托盘恢复静态
                except OSError:
                    pass
                for hicon in self._frames[1:]:  # 释放多余帧的句柄（防 GDI 泄漏）
                    user32.DestroyIcon(hicon)
                self._frames = self._frames[:1]

        def _remove(self):  # 摘除图标并释放句柄
            if self._hwnd is not None:
                if self._anim_timer:  # 动画还在跑：先停掉定时器
                    user32.KillTimer(self._hwnd, self._anim_timer)
                    self._anim_timer = 0
                data = NOTIFYICONDATAW()
                data.cbSize = ctypes.sizeof(data)  # 必填：结构大小
                data.hWnd = self._hwnd  # NIM_DELETE 只需要 hWnd + uID
                data.uID = self._uid
                try:
                    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))  # 从托盘移除图标
                except Exception:  # 可能已被系统清掉
                    pass
            self._added = False
            for hicon in self._frames:  # 销毁所有帧（含静态帧）的图标句柄
                user32.DestroyIcon(hicon)
            self._frames = []
            self._hicon = None
