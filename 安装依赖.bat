@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在安装依赖（第一次大约 1-3 分钟）...
echo.
where py >nul 2>&1 && (set PY=py -3) || (set PY=python)
%PY% --version >nul 2>&1
if errorlevel 1 (
    echo [出错] 没找到 Python。请先到 https://www.python.org/downloads/ 安装，
    echo        安装时一定勾选 "Add python.exe to PATH"，装完重新双击本文件。
    pause
    exit /b 1
)
%PY% -m pip install --upgrade -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo.
    echo 清华镜像失败，改用官方源再试一次...
    %PY% -m pip install --upgrade -r requirements.txt
)
echo.
if errorlevel 1 (
    echo [出错] 依赖没装上，把上面的报错截图发到 GitHub Issues。
) else (
    echo [完成] 现在双击 run.pyw 启动指示灯。
)
pause
