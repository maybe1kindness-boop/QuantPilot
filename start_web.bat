@echo off
chcp 65001 >nul
title QuantPilot - Web 管理端
cd /d "%~dp0"

:: 代码中含 emoji/中文, 必须开启 UTF-8, 否则一打印就崩溃
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

:: 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python, 请先安装 Python 3.10+ 并勾选 "Add to PATH"
    echo 下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

:: 首次运行: 创建虚拟环境
if not exist ".venv\Scripts\python.exe" (
    echo [首次运行] 正在创建虚拟环境...
    python -m venv .venv
    if errorlevel 1 (
        echo [错误] 虚拟环境创建失败
        pause
        exit /b 1
    )
)
set PY=.venv\Scripts\python.exe

:: 首次运行: 安装依赖
if not exist ".venv\.deps_web_installed" (
    echo [首次运行] 正在安装依赖, 视网速约需 3-5 分钟, 请勿关闭窗口...
    %PY% -m pip install --upgrade pip -q
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败, 请检查网络后重新双击本文件重试
        pause
        exit /b 1
    )
    type nul > .venv\.deps_web_installed
)

:: 首次运行: 从模板生成本地配置(不填钉钉 webhook 也能正常使用, 仅无消息推送)
if not exist "config\config.yaml" (
    copy "config\config.yaml.template" "config\config.yaml" >nul
    echo [提示] 已生成 config\config.yaml, 如需钉钉告警请编辑填写
)

echo ============================================
echo   QuantPilot Web 管理端正在启动...
echo   稍后浏览器将自动打开 http://127.0.0.1:5001
echo   若页面未自动打开, 请稍后手动访问
echo   关闭本窗口即停止服务
echo ============================================
start "" cmd /c "timeout /t 6 /nobreak >nul & start http://127.0.0.1:5001"
%PY% web_server.py

pause
