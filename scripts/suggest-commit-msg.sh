#!/usr/bin/env bash
set -euo pipefail

# prepare-commit-msgフック用スクリプト。
# `git commit`(-mなし・エディタが開くケース)でのみ、staged差分からClaude Code CLI(headless)に
# コミットメッセージ案を提案させ、エディタの下書きとして注入する。
#
# 前提:
#   - ブランチ名に Issue番号が含まれていること(例: feature/12-lint-before-commit)
#     読み取れない場合は (#ISSUE_NUMBER) のプレースホルダーを残す
#   - Claude Code CLI(`claude`)が未導入の場合は何もせず、通常通り空のエディタにフォールバックする
#     (Claude未使用のメンバーでもcommit自体は問題なく行える)

MSG_FILE="$1"

# lefthookはprepare-commit-msgフックの第2引数(コミットソース: message/template/merge/squash/commit)を
# {2}プレースホルダーで渡せない(実測: 置換されず文字列"{2}"のまま渡ってくる)ため、
# 代わりにメッセージファイルの中身を見て判定する。
# -m/-F指定時やmerge/squash/amend時は、この時点で既にコメント行以外の実質的な内容が
# 書き込まれているため、それを検出したらAI提案を差し込まずスキップする。
grep -qvE '^[[:space:]]*(#|$)' "${MSG_FILE}" && exit 0

# claude CLI未導入の場合は何もしない(通常通り空のエディタにフォールバック)
command -v claude >/dev/null 2>&1 || exit 0

DIFF="$(git diff --cached)"
[ -n "${DIFF}" ] || exit 0

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
ISSUE_NUM="$(echo "${BRANCH}" | grep -oE '[0-9]+' | head -1 || true)"

if [ -n "${ISSUE_NUM}" ]; then
  ISSUE_HINT="Issue番号は #${ISSUE_NUM} を使うこと。"
else
  ISSUE_HINT="ブランチ名からIssue番号を特定できなかったため、末尾は (#ISSUE_NUMBER) というプレースホルダーのままにすること。"
fi

PROMPT="以下のgit diffの内容から、Conventional Commits形式のコミットメッセージを提案してください。
形式: <type>(<scope>): <subject> (#<issue番号>)
- type は feat/fix/docs/style/refactor/perf/test/build/ci/chore/revert のいずれか
- scopeは省略可
- 本文やフッターは付けず、必ず1行のみ
- 前置きや説明・コードブロックは不要。コミットメッセージの文字列だけを1行で出力すること
${ISSUE_HINT}

--- git diff (staged) ---
${DIFF}
"

# --model は日付付きの具体的なモデルIDではなく "haiku" エイリアスを指定する。
# エイリアスにしておくことで、Anthropic側でモデル世代が更新されても
# 常にその時点のHaiku相当モデルに自動追従し、日付付きIDの廃止で動かなくなることを防ぐ。
SUGGESTION="$(claude -p "${PROMPT}" --model haiku 2>/dev/null | head -1 || true)"

[ -n "${SUGGESTION}" ] || exit 0

{
  echo "${SUGGESTION}"
  echo ""
  echo "# ↑ Claude Haikuによる提案です。内容を確認し、必要に応じて編集してください。"
  cat "${MSG_FILE}"
} > "${MSG_FILE}.tmp"
mv "${MSG_FILE}.tmp" "${MSG_FILE}"
