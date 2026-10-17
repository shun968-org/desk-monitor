#!/usr/bin/env bash
#
# desk-monitor を Raspberry Pi の ~/desk-monitor/ に配置し、systemd ユーザーサービスを再起動する。
#
#   1. 実行に必要なファイルを rsync で配置する(.venv は Pi 側のものを残す)
#   2. (display のみ)requirements が前回から変わっていれば ~/desk-monitor/.venv を作成・更新する
#   3. deploy/systemd/<role>/*.service を ~/.config/systemd/user/ に置き、有効化して再起動する。
#      ほかの role の desk-monitor-*.service が残っていれば止めて消す(役割を入れ替えたとき用)
#
# 使い方: scripts/deploy.sh [host] [role]
#   host: ~/.ssh/config の Host 名(既定: pi_a)
#   role: display  円形 LCD の表示プロセスとトレンド監視(pi_a の既定)
#         camera   カメラ配信サーバ(pi_b の既定)
#
# 物体検出とダッシュボード(role hub)は開発 PC で動かす: scripts/deploy-hub.sh

set -euo pipefail

host="${1:-pi_a}"
case "$host" in
  pi_b) default_role=camera ;;
  *) default_role=display ;;
esac
role="${2:-$default_role}"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
dest="desk-monitor"

cd "$repo_root"
# 配置するもの(開発用の設定やドキュメントは送らない)
case "$role" in
  display)
    files=(src assets config requirements.txt requirements-pi.txt deploy/systemd/display)
    ;;
  camera)
    files=(src deploy/systemd/camera)
    ;;
  *)
    echo "不明な role: $role(display / camera)" >&2
    exit 1
    ;;
esac

echo "==> $host:~/$dest に配置($role)"
# 配置する内容を一時ディレクトリに組み立ててから丸ごと同期し、リポジトリから消した
# ファイルは Pi からも消す。.venv/ と、実行時に作られるもの(lgpio の通知用パイプ
# .lgd-nfy* など)は Pi 側にしかないので消さないよう保護する。
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
rsync -a --relative --exclude='__pycache__/' "${files[@]}" "$stage/"
rsync -az --delete \
  --filter='P .venv/' --filter='P __pycache__/' --filter='P .lgd-nfy*' \
  "$stage/" "$host:$dest/"

echo "==> venv を確認し、systemd ユニットを更新"
ssh "$host" bash -s -- "$dest" "$role" <<'REMOTE'
set -euo pipefail
cd "$HOME/$1"
role="$2"

if [[ "$role" == display ]]; then
  if [[ ! -d .venv ]]; then
    # picamera2 の依存の libcamera など apt のパッケージを使うため system-site-packages を有効にする
    python3 -m venv --system-site-packages .venv
  fi
  stamp=.venv/.requirements.sha256
  if ! sha256sum --quiet -c "$stamp" 2>/dev/null; then
    .venv/bin/pip install -q -r requirements-pi.txt
    sha256sum requirements.txt requirements-pi.txt > "$stamp"
  fi
fi

units=()
for f in "deploy/systemd/$role"/*.service; do
  install -m 0644 -D "$f" "$HOME/.config/systemd/user/$(basename "$f")"
  units+=("$(basename "$f")")
done
# この role に含まれない desk-monitor のユニット(以前の役割のもの)は止めて消す
for f in "$HOME/.config/systemd/user"/desk-monitor-*.service; do
  [[ -e "$f" ]] || continue
  name="$(basename "$f")"
  if [[ " ${units[*]} " != *" $name "* ]]; then
    echo "以前の役割のユニットを削除: $name"
    systemctl --user disable --now "$name" 2>/dev/null || true
    rm -f "$f"
  fi
done
systemctl --user daemon-reload
systemctl --user enable "${units[@]}" >/dev/null
systemctl --user restart "${units[@]}"
sleep 3
systemctl --user --no-pager --lines=0 status "${units[@]}" | grep -E '^\S|Active:' || true
REMOTE
