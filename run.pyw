# -*- coding: utf-8 -*-
"""源码运行入口：Windows 双击本文件即可（不弹黑窗口）；Mac 双击「启动.command」。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import obs_mic_light
except ImportError as e:
    msg = (f"缺少依赖：{e.name}\n\n"
           "请先运行一次「安装依赖」：\n"
           "  Windows：双击 安装依赖.bat\n"
           "  Mac：双击 安装依赖.command")
    try:
        import tkinter
        from tkinter import messagebox
        tkinter.Tk().withdraw()
        messagebox.showerror("OBS 麦克风指示灯", msg)
    except Exception:
        print(msg)
    sys.exit(1)

sys.exit(obs_mic_light.main())
