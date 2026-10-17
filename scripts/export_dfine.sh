#!/usr/bin/env bash
#
# ハブ(desk_monitor.hub)の物体検出で使う D-FINE を ONNX に書き出し、models/dfine_<種類>.onnx に置く。
# Docker コンテナ内で書き出す。
#
#   l365: Objects365 の 365 種類(AP 44.7)。models/dfine_l_obj365.onnx
#   x365: Objects365 の 365 種類(AP 49.5)。models/dfine_x_obj365.onnx
#   l:    COCO の 80 種類(Objects365 で事前学習し COCO で仕上げたもの、AP 57.3)。models/dfine_l_obj2coco.onnx
#   x:    同(AP 59.3)。models/dfine_x_obj2coco.onnx
#
# 重みは Objects365 データセットの利用条件を受ける可能性があり、商用利用できるとは限らない(D-FINE の README)。
#
# 使い方: scripts/export_dfine.sh [l365|x365|l|x ...]   (既定: l365)
# 反映: ~/.config/desk-monitor/desk-monitor.env の DESK_MONITOR_MODEL で指定し、scripts/deploy-hub.sh

set -euo pipefail

if [[ $# -gt 0 ]]; then sizes=("$@"); else sizes=(l365); fi
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# 種類 → 重みのファイル名 / 設定ファイル / 出力名
declare -A weights=([l365]=dfine_l_obj365_e25 [x365]=dfine_x_obj365 [l]=dfine_l_obj2coco_e25 [x]=dfine_x_obj2coco)
declare -A configs=([l365]=l_obj365 [x365]=x_obj365 [l]=l_obj2coco [x]=x_obj2coco)
exports=""
for size in "${sizes[@]}"; do
  [[ -n "${weights[$size]:-}" ]] || { echo "l365 / x365 / l / x のいずれかを指定してください: $size" >&2; exit 1; }
  w="${weights[$size]}"
  exports+="wget -q https://github.com/Peterande/storage/releases/download/dfinev1.0/$w.pth && "
  exports+="python tools/deployment/export_onnx.py -c configs/dfine/objects365/dfine_hgnetv2_${configs[$size]}.yml -r $w.pth && "
  exports+="cp $w.onnx /out/dfine_${configs[$size]}.onnx && "
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
  mv "$work/dfine_${configs[$size]}.onnx" "$repo_root/models/"
  echo "書き出し完了: $repo_root/models/dfine_${configs[$size]}.onnx"
done
