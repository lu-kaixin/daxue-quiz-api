@echo off
chcp 65001 >nul
title 大学题库服务（关闭此窗口 = 停止服务）
cd /d "%~dp0"

echo ============================================
echo   大学题库本地服务
echo ============================================
echo.

if not exist "server.py" (
    echo [错误] 当前目录下没有 server.py
    echo        请把本文件放到 server.py 所在的文件夹里再运行。
    echo.
    pause
    exit /b 1
)

echo [1/3] 检查端口 8765 是否已被占用...
netstat -ano | findstr ":8765" | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo       端口已被占用 —— 说明服务可能已经在运行了。
    echo       直接用浏览器打开下面的地址确认：
    echo       http://127.0.0.1:8765/health
    echo.
    echo       如果那个窗口还开着，不用重复启动，直接去配 OCS 就行。
    echo.
    pause
    exit /b 0
)
echo       端口空闲，可以启动。
echo.

echo [2/3] 启动服务，请勿关闭本窗口...
start "" http://127.0.0.1:8765/health
python server.py

echo.
echo ============================================
echo   服务已停止。
echo   如果上面出现红色报错，把报错内容发出来。
echo ============================================
pause
