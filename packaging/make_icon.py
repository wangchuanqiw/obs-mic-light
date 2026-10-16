"""生成 assets/icon.png（1024x1024）。只在换图标时手动跑一次。"""
import os
import sys

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen

app = QGuiApplication(sys.argv)
N = 1024
img = QImage(N, N, QImage.Format_ARGB32)
img.fill(Qt.transparent)
p = QPainter(img)
p.setRenderHint(QPainter.Antialiasing)
p.setPen(Qt.NoPen)
p.setBrush(QColor("#1f2329"))
p.drawRoundedRect(QRectF(100, 100, 824, 824), 185, 185)          # macOS 风格圆角底板
p.setBrush(QColor("#2ecc71"))
p.setPen(QPen(QColor(255, 255, 255, 60), 18))
p.drawEllipse(QRectF(262, 262, 500, 500))                          # 绿点
p.setPen(Qt.NoPen)
p.setBrush(QColor("#e74c3c"))
p.drawEllipse(QRectF(640, 640, 170, 170))                          # 右下角小红点
p.end()
out = os.path.join(os.path.dirname(__file__), "..", "assets", "icon.png")
img.save(out)
print("saved", os.path.abspath(out))
