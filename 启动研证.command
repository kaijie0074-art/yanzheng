#!/bin/zsh
set -eu
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
cd "${0:A:h}"
if ! command -v uv >/dev/null 2>&1; then
  printf '需要先安装 uv（Python环境管理器）。安装后重新双击此文件。\n官方地址：https://docs.astral.sh/uv/getting-started/installation/\n'
  read -r '?按回车关闭'
  exit 1
fi
if ! uv run --locked --python 3.12 python scripts/start_research.py; then
  printf '\n启动没有完成；请保留上方错误信息。现有研究数据未删除。\n'
  read -r '?按回车关闭'
  exit 1
fi
