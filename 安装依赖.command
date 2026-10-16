#!/bin/bash
cd "$(dirname "$0")"
echo "正在安装依赖（第一次大约 1-3 分钟）..."
echo
if ! command -v python3 >/dev/null 2>&1; then
  echo "[出错] 没找到 python3。请到 https://www.python.org/downloads/ 下载安装，装完重新双击本文件。"
  read -n 1 -s -r -p "按任意键关闭"; exit 1
fi
python3 -m pip install --user --upgrade -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple \
  || python3 -m pip install --user --upgrade -r requirements.txt
if [ $? -eq 0 ]; then
  echo; echo "[完成] 现在双击「启动.command」启动指示灯。"
else
  echo; echo "[出错] 依赖没装上，把上面的报错截图发到 GitHub Issues。"
fi
read -n 1 -s -r -p "按任意键关闭"
