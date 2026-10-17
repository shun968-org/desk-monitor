#!/usr/bin/env bash
#
# ハブ(desk_monitor.hub、開発 PC)の物体検出で使う YOLO11 を ONNX に書き出し、models/<モデル名>.onnx に置く。
# 開発機の Python 環境を汚さないよう Docker コンテナ内で ultralytics を使う。
#
# 使い方: scripts/export_yolo.sh [モデル名 ...]   (既定: yolo11m。例: yolo11n yolo11s yolo11m。ハブの既定は D-FINE-L: scripts/export_dfine.sh)
#   入力サイズは環境変数 IMGSZ(既定 640)
# 反映: scripts/deploy-hub.sh(models/ をコンテナにマウントしている。使うモデルは
#       ~/.config/desk-monitor/desk-monitor.env の DESK_MONITOR_MODEL で指定する)

set -euo pipefail

IMGSZ="${IMGSZ:-640}"
if [[ $# -gt 0 ]]; then models=("$@"); else models=(yolo11m); fi
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

exports=""
for m in "${models[@]}"; do
  [[ "$m" =~ ^yolo[0-9a-z]+$ ]] || { echo "モデル名が不正です: $m" >&2; exit 1; }
  exports+="yolo export model=$m.pt format=onnx imgsz=$IMGSZ opset=17 simplify=True dynamic=False && "
done

docker run --rm -v "$work:/w" -w /w python:3.12-slim sh -c "
  apt-get update -qq && apt-get install -y -qq libgl1 libglib2.0-0 >/dev/null
  pip install -q torch torchvision --index-url https://download.pytorch.org/whl/cpu
  pip install -q ultralytics onnx onnxslim
  ${exports} chown $(id -u):$(id -g) *.onnx
"

mkdir -p "$repo_root/models"
for m in "${models[@]}"; do
  mv "$work/$m.onnx" "$repo_root/models/$m.onnx"
  echo "書き出し完了: $repo_root/models/$m.onnx"
done
