"""生成 README 用的说明图：assets/states.png（三种颜色）和 assets/menu.png（右键菜单）。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import obs_mic_light as m  # noqa: E402

app = QApplication(sys.argv)
OUT = os.path.join(ROOT, "assets")

# ---- 三种颜色
W, H = 1200, 420
img = QImage(W, H, QImage.Format_ARGB32)
img.fill(QColor("#16181d"))
p = QPainter(img)
p.setRenderHint(QPainter.Antialiasing)
font = QFont("PingFang SC")
items = [(m.GREEN, "绿", "麦克风开着"), (m.RED, "红", "静音中"), (m.GRAY, "灰", "没连上 OBS")]
for i, (color, word, desc) in enumerate(items):
    cx = W / 6 * (2 * i + 1)
    p.drawPixmap(int(cx - 70), 70, m.dot_pixmap(color, 140))
    p.setPen(QColor("#ffffff"))
    font.setPixelSize(54); font.setBold(True); p.setFont(font)
    p.drawText(QRectF(cx - 200, 240, 400, 70), Qt.AlignCenter, word)
    p.setPen(QColor("#aab0bb"))
    font.setPixelSize(36); font.setBold(False); p.setFont(font)
    p.drawText(QRectF(cx - 200, 315, 400, 60), Qt.AlignCenter, desc)
p.end()
img.save(os.path.join(OUT, "states.png"))

# ---- 右键菜单（用假数据填一个 Controller 的菜单）
class Fake:
    cfg = {"input_name": ""}
    state, detail, muted = m.ST_OK, "麦克风", False
    input_list = [("麦克风", True), ("桌面音频", False)]
    dot = type("D", (), {"dot_size": 36})()
    def __getattr__(self, _):
        return lambda *a, **k: None

fake = Fake()
menu = m.QMenu()
fake.menu = menu
m.Controller.build_menu(fake)
menu.adjustSize()
menu.grab().scaled(menu.width() * 2, menu.height() * 2, Qt.KeepAspectRatio,
                   Qt.SmoothTransformation).save(os.path.join(OUT, "menu.png"))
print("ok")
