#!/usr/bin/env bash
#
# ブランチ命名規則 <type>/<issue番号>-<slug> を検証する。
# lefthook の pre-commit フックから呼び出される想定だが、単体でも実行できる。
#
# 例: feature/7-sandbox-dev-environment, fix/12-network-allowlist-bug

set -euo pipefail

PATTERN='^(feature|fix|docs|chore|refactor|test)/[0-9]+-[a-z0-9-]+$'
DEFAULT_BRANCHES=("main" "master")

branch="$(git rev-parse --abbrev-ref HEAD)"

for default in "${DEFAULT_BRANCHES[@]}"; do
  if [[ "$branch" == "$default" ]]; then
    exit 0
  fi
done

if [[ ! "$branch" =~ $PATTERN ]]; then
  echo "エラー: ブランチ名 '$branch' が命名規則 <type>/<issue番号>-<slug> に一致しません。" >&2
  echo "  type は feature/fix/docs/chore/refactor/test のいずれか" >&2
  echo "  例: feature/7-sandbox-dev-environment" >&2
  exit 1
fi
