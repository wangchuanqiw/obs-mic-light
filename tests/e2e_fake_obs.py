"""端到端测试：起一个"假 OBS"，用源码启动指示灯，检查它连接、认证、跟随静音 / 改名、断线重连。

用法：python tests/e2e_fake_obs.py      （需要先装 requirements.txt 和 tests/requirements.txt）
CI 在干净的 Windows / macOS 虚拟机上跑它，相当于"从零安装 + 运行"的验收。
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
import time

from websockets.sync.server import serve

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOG = os.path.join(ROOT, "_log.txt")
PORT = 14455
PASSWORD = "testpw"
SALT, CHALLENGE = "c2FsdA==", "Y2hhbGxlbmdl"


class FakeObs:
    def __init__(self):
        self.mic = "Mic/Aux"
        self.muted = False
        self.conn = None

    def expected_auth(self):
        secret = base64.b64encode(hashlib.sha256((PASSWORD + SALT).encode()).digest())
        return base64.b64encode(hashlib.sha256(secret + CHALLENGE.encode()).digest()).decode()

    def handler(self, conn):
        conn.send(json.dumps({"op": 0, "d": {"obsWebSocketVersion": "5.5.0", "rpcVersion": 1,
                                             "authentication": {"salt": SALT, "challenge": CHALLENGE}}}))
        ident = json.loads(conn.recv())
        if ident["d"].get("authentication") != self.expected_auth():
            conn.close(4009, "Authentication failed")
            return
        conn.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
        self.conn = conn
        try:
            for raw in conn:
                d = json.loads(raw)["d"]
                rtype, data = d["requestType"], d.get("requestData") or {}
                ok, resp = True, {}
                if rtype == "GetSpecialInputs":
                    resp = {"desktop1": None, "desktop2": None, "mic1": self.mic,
                            "mic2": None, "mic3": None, "mic4": None}
                elif rtype == "GetInputList":
                    resp = {"inputs": [
                        {"inputName": self.mic, "inputKind": "wasapi_input_capture",
                         "unversionedInputKind": "wasapi_input_capture"},
                        {"inputName": "Desktop Audio", "inputKind": "wasapi_output_capture",
                         "unversionedInputKind": "wasapi_output_capture"},
                        {"inputName": "Game Capture", "inputKind": "game_capture",
                         "unversionedInputKind": "game_capture"}]}
                elif rtype == "GetInputMute":
                    ok = data.get("inputName") == self.mic
                    resp = {"inputMuted": self.muted}
                conn.send(json.dumps({"op": 7, "d": {
                    "requestType": rtype, "requestId": d["requestId"],
                    "requestStatus": {"result": ok, "code": 100 if ok else 600},
                    "responseData": resp}}))
        except Exception:
            pass
        finally:
            self.conn = None

    def event(self, etype, data):
        self.conn.send(json.dumps({"op": 5, "d": {"eventType": etype, "eventIntent": 8,
                                                  "eventData": data}}))


def read_log():
    try:
        with open(LOG, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def wait_for(text, timeout=30, count=1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if read_log().count(text) >= count:
            print(f"  ✓ {text}")
            return
        time.sleep(0.2)
    raise AssertionError(f"等了 {timeout}s 也没在日志里看到：{text}")


def main():
    for name in ("_log.txt", "_log.old.txt", ".lock", ".light_pos.json"):
        try:
            os.remove(os.path.join(ROOT, name))
        except OSError:
            pass
    with open(os.path.join(ROOT, "config.txt"), "w", encoding="utf-8") as f:
        f.write(f"password={PASSWORD}\nhost=127.0.0.1\nport={PORT}\n")

    obs = FakeObs()
    server = serve(obs.handler, "127.0.0.1", PORT)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
    app = subprocess.Popen([sys.executable, os.path.join(ROOT, "run.pyw")], cwd=ROOT, env=env)
    try:
        print("1. 连接 + 认证 + 自动识别麦克风")
        wait_for("状态: ok Mic/Aux")
        print("2. 静音事件")
        obs.muted = True
        obs.event("InputMuteStateChanged", {"inputName": "Mic/Aux", "inputMuted": True})
        wait_for("[事件] Mic/Aux 静音=True")
        print("3. OBS 里改名后继续跟随")
        obs.mic = "我的麦"
        obs.event("InputNameChanged", {"oldInputName": "Mic/Aux", "inputName": "我的麦"})
        wait_for("状态: ok 我的麦")
        obs.muted = False
        obs.event("InputMuteStateChanged", {"inputName": "我的麦", "inputMuted": False})
        wait_for("[事件] 我的麦 静音=False")
        print("4. OBS 断开 → 变灰 → 自动重连")
        obs.conn.close()
        wait_for("状态: offline")
        wait_for("已连上 OBS", count=2)
        wait_for("状态: ok 我的麦", count=2)
        assert app.poll() is None, "程序意外退出了"
        print("\n全部通过 ✅")
    except AssertionError as e:
        print(f"\n失败 ❌ {e}\n----- 日志 -----\n{read_log()}")
        sys.exit(1)
    finally:
        app.kill()
        server.shutdown()
        os.remove(os.path.join(ROOT, "config.txt"))


if __name__ == "__main__":
    main()
