#!/usr/bin/env bash
#
# show_face.py の物体検出で使う YOLO11n を ONNX に書き出し、models/yolo11n.onnx に置く。
# 開発機の Python 環境を汚さないよう Docker コンテナ内で ultralytics を使う。
# Pi 3 で推論できる速さにするため、入力サイズは 256x256 にしている。
#
# 使い方: scripts/export_yolo.sh
# Pi への配置: scp models/yolo11n.onnx pi_a:~/models/

set -euo pipefail

IMGSZ=256
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
