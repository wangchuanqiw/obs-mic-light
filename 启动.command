#!/bin/bash
# Mac 双击启动（源码版）。启动后这个终端窗口可以直接关掉，指示灯会继续运行。
cd "$(dirname "$0")"
nohup python3 run.pyw >/dev/null 2>&1 &
echo "指示灯已启动，看屏幕右下角的小圆点。这个窗口可以关掉了。"
