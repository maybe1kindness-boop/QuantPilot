#!/bin/bash
# QuantPilot Web 管理端 - macOS 双击启动
# 首次运行自动创建虚拟环境并安装依赖, 启动后自动打开浏览器
cd "$(dirname "$0")" || exit 1

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

# 检查 Python
if ! command -v python3 >/dev/null 2>&1; then
    echo "[错误] 未检测到 python3, 请先安装 Python 3.10+ (https://www.python.org/downloads/)"
    read -n 1 -s -r -p "按任意键退出..."
    exit 1
fi

# 首次运行: 创建虚拟环境
if [ ! -x ".venv/bin/python" ]; then
    echo "[首次运行] 正在创建虚拟环境..."
    python3 -m venv .venv || { echo "[错误] 虚拟环境创建失败"; read -n 1 -s -r -p "按任意键退出..."; exit 1; }
fi
PY=.venv/bin/python

# 首次运行: 安装依赖
if [ ! -f ".venv/.deps_web_installed" ]; then
    echo "[首次运行] 正在安装依赖, 视网速约需 3-5 分钟..."
    "$PY" -m pip install --upgrade pip -q
    "$PY" -m pip install -r requirements.txt \
        || { echo "[错误] 依赖安装失败, 请检查网络后重新双击本文件"; read -n 1 -s -r -p "按任意键退出..."; exit 1; }
    touch .venv/.deps_web_installed
fi

# 首次运行: 从模板生成本地配置(不填钉钉 webhook 也能正常使用, 仅无消息推送)
if [ ! -f "config/config.yaml" ]; then
    cp config/config.yaml.template config/config.yaml
    echo "[提示] 已生成 config/config.yaml, 如需钉钉告警请编辑填写"
fi

echo "============================================"
echo "  QuantPilot Web 管理端正在启动..."
echo "  稍后浏览器将自动打开 http://127.0.0.1:5001"
echo "  关闭本窗口即停止服务"
echo "============================================"
( sleep 4; open "http://127.0.0.1:5001" 2>/dev/null ) &
exec "$PY" web_server.py
