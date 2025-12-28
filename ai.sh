#!/usr/bin/env bash
# ai-assistant-cli 実行スクリプト

# スクリプトのディレクトリに移動（どこから呼んでもOKにするため）
cd "$(dirname "$0")" || exit 1

# venv を有効化
if [ -f ".venv/bin/activate" ]; then
  source .venv/bin/activate
fi

# 実行
python ai.py "$@"
