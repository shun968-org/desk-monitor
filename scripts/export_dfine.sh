#!/usr/bin/env bash
#
# ハブ(desk_monitor.hub)の物体検出で使う D-FINE(Objects365 で事前学習し COCO で仕上げたもの)を
# ONNX に書き出し、models/dfine_<size>_obj2coco.onnx に置く。Docker コンテナ内で書き出す。
#
#   l: COCO AP 57.3(開発 PC の CPU で 1 回 約 0.3〜0.4 秒)
#   x: COCO AP 59.3(同 約 0.7〜0.9 秒)
#
# 重みは Objects365 データセットの利用条件を受ける可能性があり、商用利用できるとは限らない(D-FINE の README)。
#
# 使い方: scripts/export_dfine.sh [l|x ...]   (既定: l)
# 反映: ~/.config/desk-monitor/desk-monitor.env の DESK_MONITOR_MODEL で指定し、scripts/deploy-hub.sh

set -euo pipefail

if [[ $# -gt 0 ]]; then sizes=("$@"); else sizes=(l); fi
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

declare -A weights=([l]=dfine_l_obj2coco_e25 [x]=dfine_x_obj2coco)
exports=""
for size in "${sizes[@]}"; do
  [[ -n "${weights[$size]:-}" ]] || { echo "l か x を指定してください: $size" >&2; exit 1; }
  w="${weights[$size]}"
  exports+="wget -q https://github.com/Peterande/storage/releases/download/dfinev1.0/$w.pth && "
  exports+="python tools/deployment/export_onnx.py -c configs/dfine/objects365/dfine_hgnetv2_${size}_obj2coco.yml -r $w.pth && "
  exports+="cp $w.onnx /out/dfine_${size}_obj2coco.onnx && "
done

# torch 2.6 以降は ONNX の書き出し方式が変わり、D-FINE の書き出しスクリプトが動かないため版を固定する
docker run --rm -v "$work:/out" python:3.11-slim bash -c "
  set -e
  apt-get update -qq && apt-get install -y -qq git wget libgl1 libglib2.0-0 >/dev/null 2>&1
  pip install -q --root-user-action=ignore torch==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cpu
  git clone -q --depth 1 https://github.com/Peterande/D-FINE.git /dfine && cd /dfine
  # 書き出しのダミー入力はバッチ 32 で、メモリが足りず落ちることがある。バッチは可変なので 1 で書き出す
  sed -i 's/torch.rand(32, 3, 640, 640)/torch.rand(1, 3, 640, 640)/' tools/deployment/export_onnx.py
  pip install -q --root-user-action=ignore -r requirements.txt onnx onnxsim
  ${exports} chown $(id -u):$(id -g) /out/*.onnx
"

mkdir -p "$repo_root/models"
for size in "${sizes[@]}"; do
  mv "$work/dfine_${size}_obj2coco.onnx" "$repo_root/models/"
  echo "書き出し完了: $repo_root/models/dfine_${size}_obj2coco.onnx"
done
