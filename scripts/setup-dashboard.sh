#!/usr/bin/env bash
#
# ダッシュボードのパスワードと、表示プロセス → ダッシュボード間の共有トークンを各 Pi に設定する。
# 秘密情報はリポジトリに置かず、各 Pi の ~/.config/desk-monitor/*.env(パーミッション 600)に書く。
#
#   ダッシュボード側(既定 pi_b): dashboard.env
#       DESK_MONITOR_DASHBOARD_USER / DESK_MONITOR_DASHBOARD_PASSWORD / DESK_MONITOR_FEED_TOKEN
#   表示側(既定 pi_a): feed.env
#       DESK_MONITOR_FEED_ADDR(ダッシュボードの IPv4:8081) / DESK_MONITOR_FEED_TOKEN
#
# パスワードを入力するので、端末(TTY)から実行する。トークンは毎回作り直す。
#
# 使い方: scripts/setup-dashboard.sh [表示側 host] [ダッシュボード側 host]

set -euo pipefail

display_host="${1:-pi_a}"
dashboard_host="${2:-pi_b}"
user="${DESK_MONITOR_DASHBOARD_USER:-desk}"

read -rsp "ダッシュボードのパスワード(ユーザー名: $user): " password; echo
read -rsp "もう一度: " confirm; echo
if [[ -z "$password" || "$password" != "$confirm" ]]; then
  echo "パスワードが空か、一致しません" >&2
  exit 1
fi
token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"

# ダッシュボード側の IPv4 アドレス(表示側の Pi は mDNS 名を解決できないため IP で指定する)
addr="$(ssh "$dashboard_host" "hostname -I | tr ' ' '\n' | grep -m1 -E '^[0-9.]+$'")"
echo "ダッシュボード: $dashboard_host ($addr)"

# systemd の EnvironmentFile 用に、値を二重引用符で囲んでエスケープする
quote() { local v="${1//\\/\\\\}"; printf '"%s"' "${v//\"/\\\"}"; }

# 秘密情報はコマンドライン引数に載せず、標準入力で渡す
write_env() {
  local host="$1" name="$2"
  ssh "$host" "umask 077 && mkdir -p ~/.config/desk-monitor && cat > ~/.config/desk-monitor/$name"
}

{
  echo "DESK_MONITOR_DASHBOARD_USER=$(quote "$user")"
  echo "DESK_MONITOR_DASHBOARD_PASSWORD=$(quote "$password")"
  echo "DESK_MONITOR_FEED_TOKEN=$(quote "$token")"
} | write_env "$dashboard_host" dashboard.env

{
  echo "DESK_MONITOR_FEED_ADDR=$(quote "$addr:8081")"
  echo "DESK_MONITOR_FEED_TOKEN=$(quote "$token")"
} | write_env "$display_host" feed.env

ssh "$dashboard_host" 'systemctl --user restart desk-monitor-dashboard 2>/dev/null || true'
ssh "$display_host" 'systemctl --user restart desk-monitor-display 2>/dev/null || true'
echo "設定しました。ブラウザで http://$addr:8080/ を開いてください(ユーザー名: $user)"
