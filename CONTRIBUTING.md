# コントリビューションガイド

## ブランチ

`<type>/<issue番号>-<slug>`(type は feature/fix/docs/chore/refactor/test)。`main` への直接コミットは対象外。
例: `feature/7-sandbox-dev-environment`

lefthook の `pre-commit` で強制される。チェックアウトごとに一度 `npm install` を実行すること
(`prepare` スクリプトが `lefthook install` を呼ぶ。devcontainer 内では `postCreateCommand` で自動実行)。

## コミットメッセージ

[Conventional Commits](https://www.conventionalcommits.org/) に従い、**タイトルのみ**とする
(本文・フッターは付けない)。末尾に Issue 参照が必須。

例: `feat(face): 表情を追加 (#7)`

commitlint が lefthook の `commit-msg` フックで強制する。

## プルリクエスト

GitHub Flow に従い、`main` へは PR 経由でマージする(squash merge)。
