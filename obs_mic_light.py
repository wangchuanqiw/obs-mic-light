# -*- coding: utf-8 -*-
"""OBS 麦克风指示灯 / OBS Mic Light

屏幕上一颗悬浮小圆点，实时显示 OBS 里麦克风的状态：
    绿 = 麦克风开着    红 = 静音    灰 = 没连上 OBS

操作：左键拖动；右键（或点托盘 / 菜单栏图标）打开菜单——选麦克风、填密码、调大小、退出。

实现要点：
  * 自己实现了一个很小的 OBS WebSocket v5 客户端：只用【一条连接、一个后台线程】，
    所有收发都在这个线程里完成，界面线程从不等待网络，OBS 卡住圆点也不会卡。
  * 静音变化靠 OBS 主动推送（InputMuteStateChanged），另外每 3 秒核对一次，
    顺便当心跳：OBS 关掉后几秒内圆点变灰，OBS 重新打开后自动重连。
"""
import base64
import datetime
import hashlib
import json
import os
import queue
import struct
import sys
import threading
import time
import uuid

APP_NAME = "OBS Mic Light"
APP_NAME_ZH = "OBS 麦克风指示灯"
VERSION = "4.0.0"
HOMEPAGE = "https://github.com/wangchuanqiw/obs-mic-light"

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")
FROZEN = getattr(sys, "frozen", False)  # 打包成 exe / app 后为 True


def _data_dir():
    """配置和日志放哪：源码运行放在程序旁边；打包版放到用户目录（app 内部不能写）。"""
    if not FROZEN:
        return os.path.dirname(os.path.abspath(__file__))
    if IS_MAC:
        base = os.path.expanduser("~/Library/Application Support")
    elif IS_WIN:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    path = os.path.join(base, "OBSMicLight")
    os.makedirs(path, exist_ok=True)
    return path


DATA_DIR = _data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "config.txt")
POS_PATH = os.path.join(DATA_DIR, ".light_pos.json")
LOG_PATH = os.path.join(DATA_DIR, "_log.txt")
LOG_MAX_BYTES = 1_000_000

# ---------------------------------------------------------------- 日志

_log_lock = threading.Lock()


def log(msg):
    line = f"[{datetime.datetime.now():%m-%d %H:%M:%S}] {msg}"
    with _log_lock:
        try:
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
                os.replace(LOG_PATH, os.path.join(DATA_DIR, "_log.old.txt"))
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
    if sys.stdout is not None:  # pythonw / 打包版没有控制台
        try:
            print(line, flush=True)
        except Exception:
            pass


# ---------------------------------------------------------------- 配置

DEFAULTS = {"password": "", "input_name": "", "size": "36", "host": "localhost", "port": "4455"}
KEY_ALIASES = {"passwd": "password", "pwd": "password", "input": "input_name",
               "source": "input_name", "name": "input_name"}
CONFIG_TEMPLATE = """\
# OBS 麦克风指示灯 配置文件
# 一般不用手动改：右键圆点，菜单里就能选麦克风、填密码、调大小。
# 手动改的话：用记事本 / 文本编辑打开，保存为 UTF-8，改完重启程序。

# OBS「工具 → WebSocket 服务器设置」里的密码；OBS 没开身份验证就留空
password={password}

# 要监控的音频源名字（必须和 OBS 里一字不差）；留空 = 自动找麦克风
input_name={input_name}

# 圆点直径（像素）
size={size}

# OBS 在哪台电脑、哪个端口；本机使用不用改
host={host}
port={port}
"""


def load_config():
    cfg = dict(DEFAULTS)
    if not os.path.exists(CONFIG_PATH):
        save_config(cfg)
        log(f"已生成配置文件: {CONFIG_PATH}")
        return cfg
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip().lower()
                key = KEY_ALIASES.get(key, key)
                if key in cfg:
                    cfg[key] = value.strip()
    except (OSError, UnicodeDecodeError) as e:
        log(f"读取 config.txt 失败（请用 UTF-8 编码保存）: {e}")
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(CONFIG_TEMPLATE.format(**cfg))
    except OSError as e:
        log(f"保存 config.txt 失败: {e}")


def cfg_int(cfg, key, lo, hi):
    try:
        return max(lo, min(hi, int(cfg.get(key, DEFAULTS[key]))))
    except ValueError:
        return int(DEFAULTS[key])


# ---------------------------------------------------------------- OBS 连接（后台线程）

import websocket  # noqa: E402  (websocket-client)
from websocket import ABNF, WebSocketTimeoutException  # noqa: E402

SUB_INPUTS = 1 << 3          # 只订阅「输入源」类事件，流量很小
CALIBRATE_SECONDS = 3        # 核对 + 心跳间隔
RETRY_SECONDS = 3            # 没连上时多久重试一次
REQUEST_TIMEOUT = 5

# 各系统里「麦克风 / 音频输入采集」的源类型
MIC_KINDS = ("wasapi_input_capture", "coreaudio_input_capture", "pulse_input_capture",
             "alsa_input_capture", "jack_input_client")
MIC_WORDS = ("mic", "麦克风", "麦", "microphone", "aux", "输入")

ST_CONNECTING, ST_OFFLINE, ST_AUTH, ST_NO_INPUT, ST_OK = (
    "connecting", "offline", "auth", "no_input", "ok")


class AuthError(Exception):
    pass


class RequestError(Exception):
    pass


class _Reconnect(Exception):
    pass


def _is_audio_kind(kind):
    kind = kind or ""
    return ("input_capture" in kind or "output_capture" in kind or "audio" in kind
            or kind in ("ffmpeg_source", "vlc_source"))


class ObsLink:
    """一个连接、一个线程的 OBS WebSocket v5 客户端。

    界面线程 → 本线程：通过 command()（放进队列）。
    本线程 → 界面线程：通过 on_* 回调；回调里只发 Qt 信号，由 Qt 切回界面线程。
    """

    def __init__(self, cfg, on_status, on_muted, on_inputs, on_renamed):
        self.cfg = cfg
        self.on_status = on_status
        self.on_muted = on_muted
        self.on_inputs = on_inputs
        self.on_renamed = on_renamed
        self.target = None
        self._ws = None
        self._cmds = queue.Queue()
        self._stop = threading.Event()
        self._need_refresh = False
        self._last_status = None
        self._thread = threading.Thread(target=self._run, name="obs-link", daemon=True)

    # ---- 给界面线程用的 ----
    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._cmds.put("stop")

    def command(self, cmd):
        """'reconnect'：改了密码/地址后重连；'refresh'：换了要监控的源。"""
        self._cmds.put(cmd)

    # ---- 线程主循环 ----
    def _status(self, state, detail=""):
        if (state, detail) != self._last_status:
            self._last_status = (state, detail)
            log(f"状态: {state} {detail}".rstrip())
            self.on_status(state, detail)

    def _run(self):
        self._status(ST_CONNECTING)
        while not self._stop.is_set():
            pause = 0
            try:
                self._session()
            except _Reconnect:
                self._status(ST_CONNECTING)
            except AuthError as e:
                self._status(ST_AUTH, str(e))
                pause = 30              # 密码不对就别狂试，等用户改密码（改了会立刻唤醒）
            except Exception as e:      # 连接被拒、超时、OBS 关闭……
                self._status(ST_OFFLINE, f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)
                pause = RETRY_SECONDS
            self._close_ws()
            if self.target is not None:
                self.target = None
                self.on_muted(None)
            if pause:
                self._wait(pause)

    def _wait(self, seconds):
        """睡一会儿；期间收到 reconnect / stop 就提前醒。"""
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            left = deadline - time.monotonic()
            if left <= 0:
                return
            try:
                cmd = self._cmds.get(timeout=left)
            except queue.Empty:
                return
            if cmd in ("reconnect", "stop"):
                return

    def _close_ws(self):
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close(timeout=1)
            except Exception:
                pass

    def _session(self):
        host = self.cfg.get("host") or "localhost"
        port = self.cfg.get("port") or "4455"
        self._ws = websocket.create_connection(f"ws://{host}:{port}", timeout=REQUEST_TIMEOUT)
        self._identify(self._ws)
        self._ws.settimeout(0.5)
        log(f"已连上 OBS ({host}:{port})")
        self._refresh()
        last_check = time.monotonic()
        while not self._stop.is_set():
            self._drain_commands()
            if self._need_refresh:
                self._refresh()
            if time.monotonic() - last_check >= CALIBRATE_SECONDS:
                last_check = time.monotonic()
                self._calibrate()
            msg = self._recv()
            if msg is not None:
                self._handle(msg)

    def _identify(self, ws):
        hello = json.loads(ws.recv())["d"]
        ident = {"rpcVersion": 1, "eventSubscriptions": SUB_INPUTS}
        if "authentication" in hello:
            password = self.cfg.get("password", "")
            if not password:
                raise AuthError("OBS 开了身份验证，需要填密码")
            a = hello["authentication"]
            secret = base64.b64encode(hashlib.sha256((password + a["salt"]).encode()).digest())
            ident["authentication"] = base64.b64encode(
                hashlib.sha256(secret + a["challenge"].encode()).digest()).decode()
        ws.send(json.dumps({"op": 1, "d": ident}))
        opcode, data = ws.recv_data()
        if opcode == ABNF.OPCODE_CLOSE:
            code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else 0
            if code == 4009:
                raise AuthError("OBS 密码不对")
            raise ConnectionError(f"OBS 拒绝了连接 (code {code})")
        if json.loads(data).get("op") != 2:
            raise ConnectionError("OBS 握手失败")

    def _drain_commands(self):
        while True:
            try:
                cmd = self._cmds.get_nowait()
            except queue.Empty:
                return
            if cmd in ("reconnect", "stop"):
                raise _Reconnect()
            if cmd == "refresh":
                self._need_refresh = True

    def _recv(self):
        """收一条消息；0.5 秒没消息返回 None。"""
        try:
            opcode, data = self._ws.recv_data()
        except WebSocketTimeoutException:
            return None
        if opcode == ABNF.OPCODE_CLOSE:
            raise ConnectionError("OBS 关闭了连接")
        if opcode != ABNF.OPCODE_TEXT:
            return None
        return json.loads(data)

    def _call(self, req_type, data=None):
        rid = uuid.uuid4().hex
        payload = {"op": 6, "d": {"requestType": req_type, "requestId": rid}}
        if data:
            payload["d"]["requestData"] = data
        self._ws.send(json.dumps(payload))
        deadline = time.monotonic() + REQUEST_TIMEOUT
        while time.monotonic() < deadline:
            msg = self._recv()
            if msg is None:
                continue
            d = msg.get("d", {})
            if msg.get("op") == 7 and d.get("requestId") == rid:
                st = d.get("requestStatus", {})
                if not st.get("result"):
                    raise RequestError(f"{req_type}: {st.get('code')} {st.get('comment') or ''}")
                return d.get("responseData") or {}
            self._handle(msg)   # 等回复期间收到的事件照常处理
        raise TimeoutError(f"OBS 没有回应 {req_type}")

    def _handle(self, msg):
        if msg.get("op") != 5:
            return
        d = msg.get("d", {})
        etype, ed = d.get("eventType"), d.get("eventData") or {}
        if etype == "InputMuteStateChanged":
            if ed.get("inputName") == self.target:
                log(f"[事件] {self.target} 静音={ed.get('inputMuted')}")
                self.on_muted(bool(ed.get("inputMuted")))
        elif etype == "InputNameChanged":
            old, new = ed.get("oldInputName"), ed.get("inputName")
            if old == self.target:
                self.target = new
            self.on_renamed(old, new)
            self._need_refresh = True
        elif etype in ("InputCreated", "InputRemoved"):
            self._need_refresh = True

    def _refresh(self):
        """重新拿源列表，决定监控哪个，并读一次当前静音状态。"""
        self._need_refresh = False
        specials = self._call("GetSpecialInputs")
        inputs = self._call("GetInputList").get("inputs", [])
        all_names = [i["inputName"] for i in inputs]
        mic_names = [specials[k] for k in ("mic1", "mic2", "mic3", "mic4") if specials.get(k)]
        mic_names += [i["inputName"] for i in inputs
                      if (i.get("unversionedInputKind") or i.get("inputKind")) in MIC_KINDS]
        mic_names += [n for n in all_names if any(w in n.lower() for w in MIC_WORDS)]
        mic_names = [n for n in dict.fromkeys(mic_names) if n in all_names]
        audio_names = [i["inputName"] for i in inputs
                       if _is_audio_kind(i.get("unversionedInputKind") or i.get("inputKind"))]
        listed = mic_names + [n for n in audio_names if n not in mic_names]
        self.on_inputs([(n, n in mic_names) for n in listed])

        wanted = self.cfg.get("input_name", "")
        if wanted and wanted in all_names:
            target = wanted
        else:
            if wanted:
                log(f"OBS 里没有叫「{wanted}」的源，改为自动选择。现有的源: {all_names}")
            target = mic_names[0] if mic_names else None

        if target is None:
            self.target = None
            self.on_muted(None)
            self._status(ST_NO_INPUT, "OBS 里没找到麦克风源")
            return
        if target != self.target:
            log(f"监控音频源: {target}" + ("" if target == wanted else "（自动选择）"))
        self.target = target
        self._calibrate()
        self._status(ST_OK, target)

    def _calibrate(self):
        if not self.target:
            return
        try:
            r = self._call("GetInputMute", {"inputName": self.target})
            self.on_muted(bool(r.get("inputMuted")))
        except RequestError:        # 源被删 / 改名了
            self._need_refresh = True


# ---------------------------------------------------------------- 界面

from PySide6.QtCore import QObject, QPoint, QRect, QSize, Qt, QTimer, QUrl, Signal, QLockFile  # noqa: E402
from PySide6.QtGui import (QActionGroup, QColor, QCursor, QDesktopServices, QGuiApplication,  # noqa: E402
                           QIcon, QPainter, QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QInputDialog, QLineEdit, QMenu, QMessageBox,  # noqa: E402
                               QSystemTrayIcon, QWidget)

GREEN = QColor("#2ecc71")
RED = QColor("#e74c3c")
GRAY = QColor("#95a5a6")
SIZES = [("小", 24), ("中", 36), ("大", 52)]

STATUS_TEXT = {
    ST_CONNECTING: "正在连接 OBS…",
    ST_OFFLINE: "没连上 OBS：OBS 没打开，或没开启「WebSocket 服务器」",
    ST_AUTH: "OBS 需要密码，或密码不对 —— 右键圆点 →「设置 OBS 密码」",
    ST_NO_INPUT: "已连上 OBS，但没找到麦克风 —— 右键圆点 →「选择麦克风」",
}


class Bridge(QObject):
    """把后台线程的回调转成 Qt 信号，自动排队回到界面线程。"""
    status = Signal(str, str)
    muted = Signal(object)          # True / False / None(未知)
    inputs = Signal(list)
    renamed = Signal(str, str)


def color_for(state, muted):
    if state != ST_OK or muted is None:
        return GRAY
    return RED if muted else GREEN


def dot_pixmap(color, size):
    """画一颗带细描边的圆点；描边让它在任何背景上都看得清。"""
    ratio = 2
    pm = QPixmap(size * ratio, size * ratio)
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen_w = max(1.5, size / 18)
    p.setPen(QPen(QColor(0, 0, 0, 110), pen_w))
    p.setBrush(color)
    r = pen_w / 2 + 0.5
    p.drawEllipse(r, r, size - 2 * r, size - 2 * r)
    p.end()
    return pm


class Dot(QWidget):
    def __init__(self, ctl, size):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowDoesNotAcceptFocus)
        self.ctl = ctl
        self.color = GRAY
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        if IS_MAC:
            self.setAttribute(Qt.WA_MacAlwaysShowToolWindow)  # 切到别的程序也不隐藏
        self._press = None
        self._drag_offset = None
        self.set_dot_size(size)

    def set_dot_size(self, size):
        self.dot_size = size
        self.setFixedSize(size, size)
        self.update()

    def set_color(self, color, tip):
        self.color = color
        self.setToolTip(tip)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.drawPixmap(0, 0, dot_pixmap(self.color, self.dot_size))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = e.globalPosition().toPoint()
            self._drag_offset = self._press - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._press is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._press is not None:
            self._press = None
            self.ctl.save_pos(self.pos())

    def contextMenuEvent(self, e):
        self.ctl.popup_menu(e.globalPos())


class Controller(QObject):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.cfg = load_config()
        self.state, self.detail, self.muted = ST_CONNECTING, "", None
        self.input_list = []
        self._asked_password = False

        self.dot = Dot(self, cfg_int(self.cfg, "size", 12, 160))
        self.dot.move(self.initial_pos())
        self.dot.show()
        if IS_MAC:
            QTimer.singleShot(0, lambda: mac_float_everywhere(self.dot))

        self.menu = QMenu()
        self.menu.aboutToShow.connect(self.build_menu)

        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon()
            self.tray.setContextMenu(self.menu)
            self.tray.setIcon(QIcon(dot_pixmap(GRAY, 32)))
            if not IS_MAC:  # Windows 左键单击也弹菜单；Mac 点一下本来就弹
                self.tray.activated.connect(self._tray_clicked)
            self.tray.show()

        self.bridge = Bridge()
        self.bridge.status.connect(self.on_status)
        self.bridge.muted.connect(self.on_muted)
        self.bridge.inputs.connect(self.on_inputs)
        self.bridge.renamed.connect(self.on_renamed)
        self.link = ObsLink(self.cfg, self.bridge.status.emit, self.bridge.muted.emit,
                            self.bridge.inputs.emit, self.bridge.renamed.emit)
        self.refresh_look()
        self.link.start()

        # 显示器插拔 / 分辨率变化后，别让圆点跑到屏幕外
        app.screenRemoved.connect(lambda _: QTimer.singleShot(500, self.keep_on_screen))

    # ---- 位置 ----
    def initial_pos(self):
        try:
            with open(POS_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            return self.clamp(QPoint(int(d["x"]), int(d["y"])))
        except (OSError, ValueError, KeyError, TypeError):
            return self.default_pos()

    def default_pos(self):
        g = QGuiApplication.primaryScreen().availableGeometry()
        s = self.dot.dot_size
        return QPoint(g.right() - s - 40, g.bottom() - s - 40)

    def clamp(self, pt):
        rect = QRect(pt, QSize(self.dot.dot_size, self.dot.dot_size))
        for screen in QGuiApplication.screens():
            if screen.availableGeometry().intersects(rect):
                return pt
        return self.default_pos()

    def keep_on_screen(self):
        self.dot.move(self.clamp(self.dot.pos()))

    def save_pos(self, pos):
        try:
            with open(POS_PATH, "w", encoding="utf-8") as f:
                json.dump({"x": pos.x(), "y": pos.y()}, f)
        except OSError:
            pass

    # ---- 后台线程发来的消息 ----
    def on_status(self, state, detail):
        self.state, self.detail = state, detail
        self.refresh_look()
        if state == ST_AUTH and not self._asked_password:
            self._asked_password = True
            QTimer.singleShot(300, self.ask_password)

    def on_muted(self, muted):
        if muted != self.muted:
            self.muted = muted
            self.refresh_look()

    def on_inputs(self, items):
        self.input_list = items

    def on_renamed(self, old, new):
        if self.cfg.get("input_name") == old:
            self.cfg["input_name"] = new
            save_config(self.cfg)
            log(f"OBS 里的源改名了：「{old}」→「{new}」，配置已同步")

    def refresh_look(self):
        color = color_for(self.state, self.muted)
        if self.state == ST_OK:
            word = {True: "静音（关）", False: "开着", None: "读取中"}[self.muted]
            tip = f"麦克风{word}：{self.detail}"
        else:
            tip = STATUS_TEXT.get(self.state, self.state)
        self.dot.set_color(color, tip)
        if self.tray:
            self.tray.setIcon(QIcon(dot_pixmap(color, 32)))
            self.tray.setToolTip(f"{APP_NAME_ZH}\n{tip}")

    # ---- 菜单 ----
    def _tray_clicked(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.popup_menu(QCursor.pos())

    def popup_menu(self, pos):
        self.menu.popup(pos)

    def build_menu(self):
        m = self.menu
        m.clear()
        if self.state == ST_OK:
            word = {True: "🔴 静音中", False: "🟢 麦克风开着", None: "读取中"}[self.muted]
            head = f"{word}（{self.detail}）"
        else:
            head = "⚪ " + STATUS_TEXT.get(self.state, self.state)
        m.addAction(head).setEnabled(False)
        m.addSeparator()

        sub = m.addMenu("选择麦克风")
        group = QActionGroup(sub)
        current = self.cfg.get("input_name", "")
        auto = sub.addAction("自动（推荐）")
        auto.setCheckable(True)
        auto.setChecked(not current)
        group.addAction(auto)
        auto.triggered.connect(lambda: self.choose_input(""))
        if self.input_list:
            sub.addSeparator()
            for name, is_mic in self.input_list:
                act = sub.addAction(("🎙 " if is_mic else "🔈 ") + name)
                act.setCheckable(True)
                act.setChecked(name == current)
                group.addAction(act)
                act.triggered.connect(lambda _=False, n=name: self.choose_input(n))
        else:
            sub.addAction("（连上 OBS 后这里会列出所有音频源）").setEnabled(False)

        size_menu = m.addMenu("圆点大小")
        cur_size = self.dot.dot_size
        for label, px in SIZES:
            act = size_menu.addAction(f"{label}（{px}）")
            act.setCheckable(True)
            act.setChecked(px == cur_size)
            act.triggered.connect(lambda _=False, v=px: self.set_size(v))

        m.addAction("设置 OBS 密码…", self.ask_password)
        m.addAction("圆点回到右下角", lambda: (self.dot.move(self.default_pos()),
                                          self.save_pos(self.dot.pos())))
        m.addSeparator()
        m.addAction("查看日志", lambda: open_path(LOG_PATH))
        m.addAction("打开配置文件夹", lambda: open_path(DATA_DIR))
        m.addAction(f"项目主页（v{VERSION}）", lambda: QDesktopServices.openUrl(QUrl(HOMEPAGE)))
        m.addSeparator()
        m.addAction("退出", self.quit)

    def choose_input(self, name):
        self.cfg["input_name"] = name
        save_config(self.cfg)
        log(f"用户选择监控: {name or '自动'}")
        self.link.command("refresh")

    def set_size(self, px):
        center = self.dot.geometry().center()
        self.dot.set_dot_size(px)
        self.dot.move(center - QPoint(px // 2, px // 2))
        self.save_pos(self.dot.pos())
        self.cfg["size"] = str(px)
        save_config(self.cfg)

    def ask_password(self):
        dlg = QInputDialog()
        dlg.setWindowTitle(APP_NAME_ZH)
        dlg.setLabelText("填 OBS 的 WebSocket 密码：\n"
                         "OBS → 工具 → WebSocket 服务器设置 → 显示连接信息 → 复制「服务器密码」\n"
                         "（OBS 里没开「身份验证」的话，这里留空）")
        dlg.setTextEchoMode(QLineEdit.Password)
        dlg.setTextValue(self.cfg.get("password", ""))
        dlg.setWindowFlag(Qt.WindowStaysOnTopHint)
        dlg.resize(460, dlg.sizeHint().height())
        if IS_MAC:
            mac_activate()
        if dlg.exec() == QInputDialog.Accepted:
            self.cfg["password"] = dlg.textValue().strip()
            save_config(self.cfg)
            log("用户更新了 OBS 密码，重新连接")
            self._asked_password = False
            self.link.command("reconnect")

    def quit(self):
        log("退出")
        self.link.stop()
        if self.tray:
            self.tray.hide()
        self.app.quit()


def open_path(path):
    QDesktopServices.openUrl(QUrl.fromLocalFile(path))


# ---------------------------------------------------------------- macOS 小修补
# 不额外装 pyobjc，用 ctypes 直接调 Objective-C 运行时。失败就跳过，不影响主功能。

def _is_cocoa():
    """只有真正的 Mac 图形界面才调系统接口（无界面测试模式下调用会崩溃）。"""
    return IS_MAC and QGuiApplication.platformName() == "cocoa"


def _objc():
    import ctypes
    import ctypes.util
    lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    lib.objc_getClass.restype = ctypes.c_void_p
    lib.objc_getClass.argtypes = [ctypes.c_char_p]
    lib.sel_registerName.restype = ctypes.c_void_p
    lib.sel_registerName.argtypes = [ctypes.c_char_p]
    addr = ctypes.cast(lib.objc_msgSend, ctypes.c_void_p).value

    def send(obj, sel, *args, restype=ctypes.c_void_p, argtypes=()):
        fn = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(addr)
        return fn(obj, lib.sel_registerName(sel), *args)

    return lib, send


def _ns_app():
    lib, send = _objc()
    return send(lib.objc_getClass(b"NSApplication"), b"sharedApplication"), send


def mac_hide_dock_icon():
    """不在 Dock 里显示 Python 火箭图标（打包版靠 Info.plist 的 LSUIElement）。"""
    import ctypes
    if not _is_cocoa():
        return
    try:
        app, send = _ns_app()
        send(app, b"setActivationPolicy:", 1, restype=ctypes.c_bool, argtypes=(ctypes.c_long,))
    except Exception as e:
        log(f"隐藏 Dock 图标失败(忽略): {e}")


def mac_activate():
    import ctypes
    if not _is_cocoa():
        return
    try:
        app, send = _ns_app()
        send(app, b"activateIgnoringOtherApps:", True, restype=None, argtypes=(ctypes.c_bool,))
    except Exception:
        pass


def mac_float_everywhere(widget):
    """所有桌面（Space）都显示，全屏程序上面也显示。"""
    import ctypes
    if not _is_cocoa():
        return
    try:
        _, send = _objc()
        view = int(widget.winId())
        window = send(view, b"window") if view else None
        if not window:
            return
        can_join_all_spaces, stationary, fullscreen_aux = 1 << 0, 1 << 4, 1 << 8
        send(window, b"setCollectionBehavior:", can_join_all_spaces | stationary | fullscreen_aux,
             restype=None, argtypes=(ctypes.c_ulong,))
        send(window, b"setHidesOnDeactivate:", False, restype=None, argtypes=(ctypes.c_bool,))
    except Exception as e:
        log(f"设置跨桌面显示失败(忽略): {e}")


# ---------------------------------------------------------------- 入口

def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)

    lock = QLockFile(os.path.join(DATA_DIR, ".lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(200):
        QMessageBox.information(None, APP_NAME_ZH, "指示灯已经在运行了（看看屏幕角落的小圆点）。")
        return 0

    log("=" * 50)
    log(f"程序启动 v{VERSION}  ({sys.platform}, Python {sys.version.split()[0]})")
    if IS_MAC:
        mac_hide_dock_icon()
    app.controller = Controller(app)  # 挂在 app 上保持引用
    log("指示灯已启动：绿=开  红=静音  灰=没连上 OBS")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
