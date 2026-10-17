#!/usr/bin/env bash
set -euo pipefail

# pre-commitフック用。stagedの差分（追加された行のみ）に秘密情報らしき文字列が
# 含まれていないかを簡易grep/パターンマッチで検査する
# (issue #7 スコープ「シークレットが誤って...書き出された場合の検知」)。
#
# 注意: これは本格的なシークレットスキャナ(gitleaks/detect-secrets等)の代替ではない。
# パターンに一致しない形式のシークレットは検出できない(誤検知・見逃しの両方がありうる)。
# 既存ファイルの内容全体ではなく、このコミットで新たに追加された行のみを対象にする
# ため、過去から存在する行は毎回再検査しない。
#
# 使い方:
#   scripts/check-no-secrets.sh [ファイル...]   引数省略時はstagedの全ファイルを対象にする

PATTERNS=(
  '-----BEGIN (RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----'
  'AKIA[0-9A-Z]{16}'                                  # AWS Access Key ID
  'ASIA[0-9A-Z]{16}'                                  # AWS一時アクセスキーID
  'AIza[0-9A-Za-z_-]{35}'                             # Google APIキー
  'gh[pousr]_[A-Za-z0-9]{36,}'                        # GitHubトークン
  'xox[baprs]-[0-9A-Za-z-]{10,}'                      # Slackトークン
  'sk-[A-Za-z0-9]{20,}'                               # OpenAI/Anthropic系シークレットキー
  '(api|access|secret)[_-]?(key|token)[[:space:]]*[:=][[:space:]]*[A-Za-z0-9/+_=-]{20,}'
)

given=("$@")
if [ "${#given[@]}" -eq 0 ]; then
  staged_list="$(mktemp)"
  trap 'rm -f "${staged_list}"' EXIT
  git diff --cached --name-only -z --diff-filter=ACMR > "${staged_list}"
  mapfile -d '' -t given < "${staged_list}"
fi

found=0
for f in "${given[@]}"; do
  [ -f "${f}" ] || continue

  # バイナリファイル(numstatの追加/削除行数が "-") は対象外
  if git diff --cached --numstat -- "${f}" | awk -F'\t' '{exit ($1=="-")?0:1}'; then
    continue
  fi

  diff_added="$(git diff --cached -U0 -- "${f}" | grep -E '^\+[^+]' || true)"
  [ -z "${diff_added}" ] && continue

  for pattern in "${PATTERNS[@]}"; do
    match="$(printf '%s\n' "${diff_added}" | grep -En -- "${pattern}" || true)"
    if [ -n "${match}" ]; then
      echo "エラー: ${f} に秘密情報らしき文字列が追加されています(パターン: ${pattern})" >&2
      printf '%s\n' "${match}" | sed 's/^/  /' >&2
      found=1
    fi
  done
done

if [ "${found}" -eq 1 ]; then
  echo "" >&2
  echo "本当に必要な変更であれば該当箇所を確認・除去すること。誤検知であれば" >&2
  echo "scripts/check-no-secrets.sh のパターンを見直すこと。" >&2
  exit 1
fi

exit 0
