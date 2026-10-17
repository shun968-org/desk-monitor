#!/usr/bin/env bash
#
# ハブ(物体検出とダッシュボード)を開発 PC の Docker で起動・更新する。
#   ダッシュボード: http://127.0.0.1:8090/
#
# 前提: scripts/setup-secrets.sh で ~/.config/desk-monitor/desk-monitor.env を作ってあること、
#       models/yolo11n.onnx があること(scripts/export_yolo.sh で作る)
#
# 使い方: scripts/deploy-hub.sh [docker compose の追加引数(例: logs -f)]

set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
compose=(docker compose -f "$repo_root/deploy/hub/compose.yaml")

if [[ $# -gt 0 ]]; then
  exec "${compose[@]}" "$@"
fi

if [[ ! -f "$HOME/.config/desk-monitor/desk-monitor.env" ]]; then
  echo "~/.config/desk-monitor/desk-monitor.env がありません。scripts/setup-secrets.sh を実行してください" >&2
  exit 1
fi
[[ -f "$repo_root/models/yolo11n.onnx" ]] || echo "注意: models/yolo11n.onnx がないため物体検出をしません" >&2
mkdir -p "$HOME/.local/share/desk-monitor"

HUB_UID="$(id -u)" HUB_GID="$(id -g)" "${compose[@]}" up -d --build
# ソースはマウントしているので、ソースだけ変えたときも再起動で反映する
HUB_UID="$(id -u)" HUB_GID="$(id -g)" "${compose[@]}" restart hub
"${compose[@]}" ps
echo "ダッシュボード: http://127.0.0.1:8090/"
