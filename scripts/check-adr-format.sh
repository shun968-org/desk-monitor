#!/usr/bin/env bash
set -euo pipefail

# pre-commitフック用。docs/adr/ 配下のADRが Nygard 形式に沿っているかを検査する。
#
# 検査する理由:
#   ADRの目的は「今どの決定が有効かを後から素早く把握できること」にある。
#   見出しの揺れとステータスの但し書き(「採用(ただし〜は保留)」等)が
#   その目的を壊しかねないため、書式は機械的に固定する。
#
# 書式の定義はどこにあるか:
#   `managing-adrs` スキル(.claude/skills/managing-adrs)。テンプレート・ステータス語彙・
#   採用済みADRの不変性の運用まで含めて、そちらが正。
#   このスクリプトはその一部（機械的に判定できる書式だけ）を検査する。
#
# 使い方:
#   scripts/check-adr-format.sh [ファイル...]   引数省略時はstagedのADRを対象にする

given=("$@")

if [ "${#given[@]}" -eq 0 ]; then
  # -z: 非ASCIIパスがクォートされて検査対象から外れるのを防ぐ
  staged_list="$(mktemp)"
  trap 'rm -f "${staged_list}"' EXIT

  if ! git diff --cached --name-only -z --diff-filter=ACMR > "${staged_list}"; then
    echo "stagedのファイル一覧を取得できなかったため中断する" >&2
    exit 1
  fi
  mapfile -d '' -t given < "${staged_list}"
fi

# 呼び出し元(lefthookのglob)は docs/adr/ 配下のMarkdownを無差別に渡してくる。
# README.md を置く場合に備えて除外する。
# 除外をファイル名の連番パターンで行わないのは、`bad-name.md` のような
# 命名違反そのものを検出対象に残すため。
targets=()
for f in "${given[@]}"; do
  case "${f}" in
    docs/adr/*|*/docs/adr/*) ;;
    *) continue ;;
  esac
  [ "$(basename "${f}")" = "README.md" ] && continue
  targets+=("${f}")
done

[ "${#targets[@]}" -gt 0 ] || exit 0

STATUS_VOCAB='提案中|採用|却下|非推奨|置換済み（ADR-[0-9]{4}）'
violations=0

report() {
  echo "NG: [$1] $2" >&2
  violations=$((violations + 1))
}

for f in "${targets[@]}"; do
  [ -f "${f}" ] || continue
  base="$(basename "${f}")"

  [[ "${base}" =~ ^[0-9]{4}-[a-z0-9-]+\.md$ ]] \
    || report adr-filename "${f}: ファイル名は 0001-kebab-case.md 形式にする"

  head -1 "${f}" | grep -qE '^# ADR-[0-9]{4}: .+' \
    || report adr-title "${f}: 1行目は '# ADR-XXXX: タイトル' にする"

  # ステータスは1語。但し書きが付くと一覧から有効性を判断できなくなる
  status_line="$(grep -m1 '^- ステータス:' "${f}" || true)"
  if [ -z "${status_line}" ]; then
    report adr-status-missing "${f}: '- ステータス:' が無い"
  elif ! echo "${status_line}" | grep -qE "^- ステータス: (${STATUS_VOCAB})$"; then
    report adr-status-vocab "${f}: ステータスは 提案中 / 採用 / 却下 / 非推奨 / 置換済み（ADR-XXXX） のいずれか1語にする(但し書きを付けない): ${status_line}"
  fi

  grep -qE '^- 日付: [0-9]{4}-[0-9]{2}-[0-9]{2}$' "${f}" \
    || report adr-date "${f}: '- 日付: YYYY-MM-DD' が無い"

  # 見出しは3つに固定する。増やすと同じ性質の記述が別の場所に散る。
  # コードフェンス内の `## ` は見出しではないため除外する
  # (ADR本文にMarkdownの例を載せると誤検知するため)
  actual_headings="$(awk '/^```/ { in_fence = !in_fence; next } !in_fence && /^## /' "${f}")"
  expected_headings=$'## コンテキスト\n## 決定\n## 結果'
  if [ "${actual_headings}" != "${expected_headings}" ]; then
    report adr-headings "${f}: 見出しは '## コンテキスト' '## 決定' '## 結果' の3つをこの順で置く(現在: $(echo "${actual_headings}" | tr '\n' ' '))"
  fi
done

if [ "${violations}" -gt 0 ]; then
  echo "" >&2
  echo "書式とテンプレートは .claude/skills/managing-adrs/SKILL.md を参照する。" >&2
  exit 1
fi
