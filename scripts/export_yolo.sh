#!/usr/bin/env bash
#
# ハブ(desk_monitor.hub、開発 PC)の物体検出で使う YOLO11n を ONNX に書き出し、models/yolo11n.onnx に置く。
# 開発機の Python 環境を汚さないよう Docker コンテナ内で ultralytics を使う。
# 入力サイズの既定は 640x640(開発 PC の CPU で毎フレーム推論できる)。
#
# 使い方: scripts/export_yolo.sh [入力サイズ(既定 640)]
# 反映: scripts/deploy-hub.sh(models/ をコンテナにマウントしている)

set -euo pipefail

IMGSZ="${1:-640}"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

docker run --rm -v "$work:/w" -w /w python:3.12-slim sh -c "
  apt-get update -qq && apt-get install -y -qq libgl1 libglib2.0-0 >/dev/null
  pip install -q torch torchvision --index-url https://download.pytorch.org/whl/cpu
  pip install -q ultralytics onnx onnxslim
  yolo export model=yolo11n.pt format=onnx imgsz=$IMGSZ opset=17 simplify=True dynamic=False
  chown $(id -u):$(id -g) yolo11n.onnx
"

mkdir -p "$repo_root/models"
mv "$work/yolo11n.onnx" "$repo_root/models/yolo11n.onnx"
echo "書き出し完了: $repo_root/models/yolo11n.onnx"
