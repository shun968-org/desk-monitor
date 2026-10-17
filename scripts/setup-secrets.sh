#!/usr/bin/env bash
#
# 機器間の共有トークンを作り、接続先と一緒に各機器の ~/.config/desk-monitor/desk-monitor.env
# (パーミッション 600)に書く。トークンはリポジトリに置かない。実行するたびに作り直す。
#
#   表示用の Pi(既定 pi_a)  : DESK_MONITOR_TOKEN, DESK_MONITOR_CAMERA_ADDR
#   カメラの Pi(既定 pi_b)  : DESK_MONITOR_TOKEN
#   この PC(ハブ)          : DESK_MONITOR_TOKEN, DESK_MONITOR_CAMERA_ADDR, DESK_MONITOR_DISPLAY_ADDR
#
# Pi は mDNS 名を解決できないため、接続先は各 Pi の IPv4 アドレスで書く。
# Pi の IP アドレスが変わったら再実行する(各サービスも再起動する)。
#
# 使い方: scripts/setup-secrets.sh [表示用の host] [カメラの host]

set -euo pipefail

display_host="${1:-pi_a}"
camera_host="${2:-pi_b}"
camera_port=8082
control_port=8083

ipv4() { ssh "$1" "hostname -I | tr ' ' '\n' | grep -m1 -E '^[0-9.]+$'"; }
display_ip="$(ipv4 "$display_host")"
camera_ip="$(ipv4 "$camera_host")"
echo "表示: $display_host ($display_ip)  カメラ: $camera_host ($camera_ip)"

token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"

# トークンはコマンドライン引数に載せず、標準入力で渡す
write_env() {
  local host="$1"
  ssh "$host" 'umask 077 && mkdir -p ~/.config/desk-monitor && cat > ~/.config/desk-monitor/desk-monitor.env'
}

printf 'DESK_MONITOR_TOKEN=%s\nDESK_MONITOR_CAMERA_ADDR=%s\n' \
  "$token" "$camera_ip:$camera_port" | write_env "$display_host"
printf 'DESK_MONITOR_TOKEN=%s\n' "$token" | write_env "$camera_host"
(
  umask 077
  mkdir -p "$HOME/.config/desk-monitor"
  printf 'DESK_MONITOR_TOKEN=%s\nDESK_MONITOR_CAMERA_ADDR=%s\nDESK_MONITOR_DISPLAY_ADDR=%s\n' \
    "$token" "$camera_ip:$camera_port" "$display_ip:$control_port" \
    > "$HOME/.config/desk-monitor/desk-monitor.env"
)

# 以前の構成(pi_b 経由のダッシュボード)の設定ファイルは使わないので消す
for host in "$display_host" "$camera_host"; do
  ssh "$host" 'rm -f ~/.config/desk-monitor/feed.env ~/.config/desk-monitor/dashboard.env'
done

ssh "$camera_host" 'systemctl --user try-restart desk-monitor-camera 2>/dev/null || true'
ssh "$display_host" 'systemctl --user try-restart desk-monitor-display 2>/dev/null || true'
if docker compose -f "$(dirname "$0")/../deploy/hub/compose.yaml" ps -q hub 2>/dev/null | grep -q .; then
  "$(dirname "$0")/deploy-hub.sh"
fi
echo "設定しました"
