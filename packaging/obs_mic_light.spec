# PyInstaller 打包配置：pyinstaller packaging/obs_mic_light.spec
# Windows 产出 dist/OBS Mic Light/OBS Mic Light.exe；Mac 产出 dist/OBS Mic Light.app
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
sys.path.insert(0, ROOT)
from obs_mic_light import VERSION  # noqa: E402

NAME = "OBS Mic Light"
ICON = os.path.join(ROOT, "assets", "icon.png")   # 装了 Pillow 会自动转成 ico / icns

# 用不到的 Qt 模块全部排除，减小体积
EXCLUDES = ["tkinter", "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick",
            "PySide6.QtPdf", "PySide6.QtSvg", "PySide6.QtOpenGL", "PySide6.QtDBus"]

a = Analysis([os.path.join(ROOT, "obs_mic_light.py")], pathex=[ROOT], excludes=EXCLUDES)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=NAME, console=False,
          icon=ICON, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)

if sys.platform == "darwin":
    app = BUNDLE(coll, name=f"{NAME}.app", icon=ICON,
                 bundle_identifier="io.github.wangchuanqiw.obsmiclight",
                 info_plist={
                     "CFBundleShortVersionString": VERSION,
                     "CFBundleVersion": VERSION,
                     "LSUIElement": True,              # 不在 Dock 显示
                     "NSHighResolutionCapable": True,
                 })
