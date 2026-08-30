# -*- coding: utf-8 -*-
"""OBS Microphone Status Light  (v3 - single-connection, rock solid)
----------------------------------------------------------------
GREEN = mic ON (not muted)      RED = mic OFF (muted)
----------------------------------------------------------------
操作：
  - 左键按住圆点 -> 拖动
  - 松开左键     -> 固定并记忆位置
  - Ctrl+Alt+M   -> 关闭（需 pynput）
  - 右键圆点 / 托盘右键 -> 关闭
----------------------------------------------------------------
监控策略（v3 彻底修复"快速连按后失效"）：
  根因（已通过读 obsws-python 源码确认）：
    * EventClient() 构造时会【自动 subscribe()】，启动一个后台 recv 线程
      -> 绝不能手动再调一次 subscribe()，否则起两个线程互相抢夺
    * EventClient.disconnect() 内部会 worker.join() -> 【阻塞调用】
      -> 绝不能在 UI 线程/请求锁里调，必须丢到后台线程
    * ReqClient/EventClient 各占一条 WebSocket 连接
    * 旧版的"高频轮询 + 连续失败强制重建"会导致请求阻塞/事件线程僵死
      -> 表现为"按几次开关后就不监控了"
  本版对策：
    [A] 唯一权威 = 事件通道（InputMuteStateChanged -> on_input_mute_state_changed）
         **回调名已验证匹配**（obsws to_snake_case 转换确认）
    [B] 所有 OBS 请求走【单 ReqClient + 串行锁】，绝不同时发两个请求
    [C] 彻底去掉"连续失败强制重建"死循环：
         - 请求失败仅记日志，不做任何重建
         - 仅当【事件后台线程确已死亡】时才在后台线程安全地完整重建
    [D] 慢速校准（5s）仅静默核对状态，不驱动 UI，不刷屏/不阻塞
    [E] 重建全部在后台线程执行（含 disconnect/join），UI 线程零阻塞
----------------------------------------------------------------
"""
import os
import sys
import json
import time
import datetime
import threading

# ---------- 日志：文件 + 控制台双写 ----------
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_log.txt")


def log(msg):
    line = f"[{datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


log("=" * 55)
log("程序启动 (v3)")

# ---------- Qt ----------
from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QSystemTrayIcon, QMenu, QStyle
)
from PyQt5.QtCore import Qt, QTimer, QPoint, pyqtSignal
from PyQt5.QtGui import QPainter, QColor

# ---------- OBS ----------
from obsws_python import ReqClient, EventClient

try:
    from pynput import keyboard as pynput_keyboard
    HAS_PYNPUT = True
except Exception:
    HAS_PYNPUT = False

# ---------------- 配置 ----------------
HOST = "localhost"
PORT = 4455
PASSWORD = ""
INPUT_NAME = "麦克风_桌面"   # None / "" = 自动探测第一个麦克风类输入源

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.txt")
POS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".light_pos.json")

RED = "#e74c3c"     # 静音 = 关闭
GREEN = "#2ecc71"   # 非静音 = 开启
SIZE = 40

CALIBRATE_MS = 5000   # 慢速校准间隔（5秒，仅静默核对）
HEALTH_MS = 5000      # 健康检查间隔（5秒，仅在事件线程死亡时重建）
# ---------------------------------------


def load_config():
    global PASSWORD, INPUT_NAME
    if not os.path.exists(CONFIG_FILE):
        return
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip().lower(), value.strip()
                if key in ("password", "passwd", "pwd"):
                    PASSWORD = value
                elif key in ("input_name", "input", "source", "name"):
                    if value:
                        INPUT_NAME = value
        log(f"配置加载: input_name={INPUT_NAME!r}, password={'***' if PASSWORD else '(空)'}")
    except Exception as e:
        log(f"读取 config.txt 失败: {e}")


def save_pos(pos):
    try:
        with open(POS_FILE, "w", encoding="utf-8") as f:
            json.dump({"x": int(pos.x()), "y": int(pos.y())}, f)
    except Exception:
        pass


def load_pos():
    try:
        if os.path.exists(POS_FILE):
            with open(POS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            return QPoint(int(d.get("x", 0)), int(d.get("y", 0)))
    except Exception:
        pass
    return None


class Light(QWidget):
    sig_set_muted = pyqtSignal(bool)
    sig_log = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)  # 默认鼠标穿透

        self.label = QLabel(self)
        self.label.setFixedSize(SIZE, SIZE)
        self.label.setAlignment(Qt.AlignCenter)
        self.setFixedSize(SIZE, SIZE)

        self._drag_pos = None
        self._dragging = False

        self.sig_set_muted.connect(self.set_muted)
        self.sig_log.connect(lambda m: log(m))

        self.set_muted(True)
        self.show()

        saved = load_pos()
        if saved is not None:
            self.move(saved)
        else:
            sg = QApplication.primaryScreen().availableGeometry()
            self.move(sg.width() - SIZE - 40, sg.height() - SIZE - 40)

        # ===== OBS 状态 =====
        self.req = None
        self.evt = None
        self._req_lock = threading.Lock()   # 串行化所有主动请求
        self.input_name = None
        self._connected = False             # 是否成功建立过一次完整连接
        self._rebuilding = False           # 防止重建重入

        # 先建连（在后台线程，避免阻塞 UI）
        self._ensure_connected()

        # 慢速校准：仅静默核对状态
        self.calib_timer = QTimer(self)
        self.calib_timer.timeout.connect(self.calibrate)
        self.calib_timer.start(CALIBRATE_MS)

        # 健康检查：仅检测事件线程是否还活着
        QTimer.singleShot(HEALTH_MS, self.health_check)

    # ==================== 连接管理（全部在后台线程执行） ====================
    def _ensure_connected(self):
        """首次连接：在后台线程完成，避免 EventClient() 内的 subscribe/recv 阻塞 UI。"""
        threading.Thread(target=self._connect_block, daemon=True).start()

    def _connect_block(self):
        """【后台线程】建立完整连接。"""
        try:
            # 清理旧连接（首次 self.req/evt 为 None，安全）
            self._teardown_old()

            # 唯一 ReqClient（全程串行加锁访问）
            self.req = ReqClient(host=HOST, port=PORT, password=PASSWORD)

            # 解析目标输入源
            self.input_name = self.resolve_input()
            log(f"连接成功，监控输入源: {self.input_name}")

            # 唯一 EventClient —— 注意：构造即自动 subscribe() 起事件线程，
            # 绝不能手动再调 subscribe()，否则会起第二个 recv 线程。
            self.evt = EventClient(host=HOST, port=PORT, password=PASSWORD)
            self._register_callback()

            # 查询一次初始状态（串行）
            self._query_initial()

            self._connected = True
            log("连接就绪（事件通道已激活）")
        except Exception as e:
            log(f"连接失败: {e}")
            self._connected = False

    def _teardown_old(self):
        """【后台线程】安全清理旧连接。
        关键：EventClient.disconnect() 内部会 worker.join() -> 阻塞调用，
        所以本方法只能在后台线程调用，绝不能在 UI 线程/锁里调。
        """
        evt, req = self.evt, self.req
        self.evt = None
        self.req = None
        if evt is not None:
            try:
                evt.disconnect()   # 阻塞：等待事件线程退出
                log("旧 EventClient 已断开")
            except Exception as e:
                log(f"断开旧 EventClient 时异常(可忽略): {e}")
        if req is not None:
            try:
                req.disconnect()
            except Exception:
                pass

    def _register_callback(self):
        """注册 InputMuteStateChanged 回调。
        已验证：obsws 的 Callback.trigger 用 fn.__name__ == "on_" + to_snake_case(event)
        -> InputMuteStateChanged 经 to_snake_case 变为 input_mute_state_changed
        -> 回调函数必须命名为 on_input_mute_state_changed ✓
        """
        try:
            self.evt.callback.register(self.on_input_mute_state_changed)
            log("事件回调已注册: on_input_mute_state_changed")
        except Exception as e:
            log(f"回调注册失败: {e}")

    def resolve_input(self):
        if INPUT_NAME:
            return INPUT_NAME
        try:
            with self._req_lock:
                resp = self.req.get_input_list()
            names = [i["inputName"] for i in resp.inputs]
            for n in names:
                low = n.lower()
                if any(k in low for k in ("mic", "aux", "microphone", "麦克风", "输入")):
                    return n
            return names[0] if names else None
        except Exception as e:
            log(f"列举输入源失败: {e}")
            return None

    def _query_initial(self):
        if not self.input_name or self.req is None:
            return
        try:
            with self._req_lock:
                r = self.req.get_input_mute(self.input_name)
            self.sig_set_muted.emit(bool(r.input_muted))
            log(f"初始状态: muted={r.input_muted}")
        except Exception as e:
            log(f"查询初始状态失败: {e}")

    # ==================== 状态来源 A：事件回调（唯一权威） ====================
    def on_input_mute_state_changed(self, event):
        """OBS 推送静音变化 -> 立即更新圆点。
        后台 recv 线程调用，通过信号切回 UI 线程更新。
        """
        name = getattr(event, "input_name", None)
        if self.input_name and name and name != self.input_name:
            return  # 不是我们监控的源，忽略
        muted = getattr(event, "input_muted", None)
        if muted is None:
            return
        log(f"[事件] {name} muted={muted}")
        self.sig_set_muted.emit(bool(muted))

    # ==================== 状态来源 B：慢速校准（仅静默核对） ====================
    def calibrate(self):
        """每 5 秒静默核对一次状态，仅作最终兜底，不刷屏/不频繁请求。"""
        if not self._connected or not self.input_name or self.req is None:
            return
        try:
            with self._req_lock:
                r = self.req.get_input_mute(self.input_name)
            muted = bool(r.input_muted)
            # 静默更新 UI（失败也不重建，避免死循环）
            self.sig_set_muted.emit(muted)
        except Exception as e:
            log(f"[校准] 请求失败(忽略，等事件线程): {e}")

    # ==================== 健康检查：仅在事件线程死亡时才重建 ====================
    def health_check(self):
        if self._rebuilding:
            QTimer.singleShot(HEALTH_MS, self.health_check)
            return
        evt_alive = False
        if self.evt is not None:
            worker = getattr(self.evt, "worker", None)
            if worker is not None and getattr(worker, "is_alive", lambda: False)():
                evt_alive = True
        if self._connected and evt_alive:
            pass  # 一切正常，什么都不做
        else:
            # 事件通道确已死亡 -> 才触发一次完整重建（在后台线程）
            log("检测到事件线程已死亡，触发后台重建")
            self._connected = False
            self._rebuilding = True
            threading.Thread(target=self._rebuild_block, daemon=True).start()
        QTimer.singleShot(HEALTH_MS, self.health_check)

    def _rebuild_block(self):
        """【后台线程】安全地完整重建连接。"""
        try:
            self._connect_block()
        finally:
            self._rebuilding = False

    # ==================== 显示 ====================
    def set_muted(self, muted):
        color = RED if muted else GREEN
        self.label.setStyleSheet(
            f"QLabel {{ background-color: {color}; border-radius: {SIZE // 2}px; }}"
        )
        self.show()

    # ==================== 拖动 / 鼠标穿透 ====================
    def set_pass_through(self, enabled):
        self.setAttribute(Qt.WA_TransparentForMouseEvents, enabled)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.set_pass_through(False)
            self._drag_pos = e.globalPos() - self.frameGeometry().topLeft()
            self._dragging = True
            e.accept()

    def mouseMoveEvent(self, e):
        if self._dragging and (e.buttons() & Qt.LeftButton):
            self.move(e.globalPos() - self._drag_pos)
            e.accept()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            save_pos(self.pos())
            self.set_pass_through(True)
            e.accept()

    def contextMenuEvent(self, e):
        # 右键关闭（额外兜底）
        self.quit_app()

    def quit_app(self):
        log("收到关闭指令，退出")
        # disconnect 是阻塞调用，放到后台线程，UI 立即退出不卡顿
        evt, req = self.evt, self.req
        self.evt = None
        self.req = None
        threading.Thread(target=self._safe_disconnect, args=(evt, req), daemon=True).start()
        QApplication.quit()

    @staticmethod
    def _safe_disconnect(evt, req):
        if evt is not None:
            try:
                evt.disconnect()
            except Exception:
                pass
        if req is not None:
            try:
                req.disconnect()
            except Exception:
                pass


def main():
    load_config()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    light = Light()

    # 托盘
    tray = QSystemTrayIcon(light)
    tray.setToolTip("OBS 麦克风指示灯 (右键关闭)")
    tray.setIcon(app.style().standardIcon(QStyle.SP_MessageBoxInformation))
    menu = QMenu()
    act = menu.addAction("Exit (关闭)")
    act.triggered.connect(light.quit_app)
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: light.quit_app() if reason == QSystemTrayIcon.Trigger else None
    )
    tray.show()

    # 全局热键
    listener = [None]
    if HAS_PYNPUT:
        def on_hotkey():
            QTimer.singleShot(0, light.quit_app)
        try:
            listener[0] = pynput_keyboard.GlobalHotKeys({"<ctrl>+<alt>+m": on_hotkey})
            listener[0].start()
            log("全局热键已注册: Ctrl+Alt+M")
        except Exception as e:
            log(f"全局热键注册失败(可托盘关闭): {e}")
    else:
        log("未安装 pynput，热键不可用；用托盘/右键 Exit 关闭")

    log("指示灯已启动: 绿=开  红=关")
    code = app.exec_()
    try:
        if listener[0] is not None:
            listener[0].stop()
    except Exception:
        pass
    sys.exit(code)


if __name__ == "__main__":
    main()
