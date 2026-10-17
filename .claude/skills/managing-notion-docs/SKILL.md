---
name: managing-notion-docs
description: 要件定義書・基本設計書をNotion上で作成・更新する。Notion公式CLI `ntn` を使い、チームスペース先頭のindexページを起点に、要件定義ツリー・基本設計ツリー（各ルートページ+8子ページ）を辿る。Notion上へのドキュメント構造の初回セットアップ、既存ページの参照・更新の両方を扱う。「要件定義をNotionに書く」「基本設計をNotionにまとめる」「Notionのドキュメントを更新する」といった依頼が該当する。
---

# 要件定義・基本設計のNotion管理

要件定義書・基本設計書はNotionで管理し、ページIDや構造はリポジトリに保存しない。検索には頼らず、
チームスペース先頭のindexページを唯一の入口にして辿る。

## 前提条件

- `ntn` コマンドが導入済み。
- 認証は **内部インテグレーションのトークン(`NOTION_API_TOKEN`)** を使う。`ntn login`(ユーザー本人のOAuth)は
  ワークスペース全体の権限になるため、エージェントからは使わない。
  - インテグレーションは最小権限(コンテンツの読み取り・更新・挿入のみ)で作り、**indexページ(とその配下)だけ**を
    共有する。共有していないページには、トークンがあってもアクセスできない。
  - トークンはホストの環境変数 `NOTION_API_TOKEN` に置き、devcontainer が `remoteEnv` で渡す。リポジトリには置かない。
  - 設定手順は [`docs/notion-agent-auth.md`](../../../docs/notion-agent-auth.md) を参照。
- 未認証エラー(`No auth token found`)や権限エラー(`object_not_found` / `restricted_resource`)が出たら処理を止め、
  ユーザーに「トークン未設定」または「対象ページがインテグレーションに共有されていない」ことを伝える。
  Claudeは認証情報を探し回らず、`ntn login` / `ntn auth token` も実行しない。
- `ntn api` の `-X DELETE` は使わない(ページの削除はユーザーがNotion上で行う)。
- `jq` が導入済み。

## チームスペース構成

テンプレートファイルのツリーに、対応するNotion上の見え方をカッコで添える。1ファイル = Notion上の1ページ。

```
templates/index.md（indexページ・チームスペース先頭）
├─ templates/01-requirements/00-root.md（要件定義・ルートページ）
│   ├─ templates/01-requirements/01-background-purpose.md（背景・目的）
│   ├─ templates/01-requirements/02-scope.md（スコープ）
│   ├─ templates/01-requirements/03-stakeholders.md（利用者・ステークホルダー）
│   ├─ templates/01-requirements/04-functional-requirements.md（機能要件）
│   ├─ templates/01-requirements/05-non-functional-requirements.md（非機能要件）
│   ├─ templates/01-requirements/06-constraints.md（制約条件）
│   ├─ templates/01-requirements/07-glossary.md（用語集）
│   └─ templates/01-requirements/08-open-issues.md（未決事項）
└─ templates/02-basic-design/00-root.md（基本設計・ルートページ）
    ├─ templates/02-basic-design/01-overview.md（概要）
    ├─ templates/02-basic-design/02-system-architecture.md（システム構成）
    ├─ templates/02-basic-design/03-functional-design.md（機能設計）
    ├─ templates/02-basic-design/04-data-design.md（データ設計）
    ├─ templates/02-basic-design/05-external-interfaces.md（外部インターフェース）
    ├─ templates/02-basic-design/06-non-functional-design.md（非機能設計）
    ├─ templates/02-basic-design/07-migration-release-plan.md（移行・リリース計画）
    └─ templates/02-basic-design/08-open-issues.md（未決事項）
```

ディレクトリ名・ファイル名の数字が作成順。各ディレクトリの `00-root.md` がルートページ、それ以外が
その子ページになる。ページタイトルは各ファイル先頭の `# 見出し` から取る。
ツリーを増減させたいときはテンプレート側だけを変える（`setup-space.sh` は `templates/` を走査するので
スクリプトの修正は要らない）。

## 進め方

対象チームスペースとそのindexページ（IDまたはURL）をユーザーに確認してから分岐する。

- **indexページがまだ無い** → 「初回セットアップ」
- **indexページがある** → 「対象ページを辿る」

## 初回セットアップ

```sh
.claude/skills/managing-notion-docs/scripts/setup-space.sh <チームスペースのトップページID または URL>
```

`templates/` 配下のツリーをまとめて作成し、indexページに各ルートページへのリンクを書き込む。
**冪等ではない** — 既にツリーがあるチームスペースで実行すると重複作成される。

## 対象ページを辿る

1. indexページからルートページのリンクを得る。

   ```sh
   ntn pages get <indexページID> | grep -oE '<page url="[^"]+">[^<]+</page>'
   ```

2. ルートページ配下の子ページ一覧から目的のページを探す。

   ```sh
   ntn api v1/blocks/<ルートページID>/children \
     | jq -r '.results[] | select(.type=="child_page") | "\(.id) \(.child_page.title)"'
   ```

3. 見つかれば「ページを更新する」へ。無ければ対応するテンプレートの内容で新規作成する。タイトル付けと
   本文書き込みは分ける（`ntn pages create` は出力形式が不定で、作成したページIDを拾えないため使わない）。

   ```sh
   ntn api v1/pages -X POST \
     parent[page_id]="<親ページID>" \
     properties[title][title][0][text][content]="<ページタイトル>"
   ntn pages edit "<応答JSONの .id>" --content "<テンプレートの内容>"
   ```

## ページを更新する

`ntn pages edit` は `--content` の全文でページ内容を置き換える（部分パッチではない）。

```sh
ntn pages get <ページID> > /tmp/current.md
# 編集して /tmp/updated.md を作る
ntn pages edit <ページID> --content "$(cat /tmp/updated.md)"
```

**子ページを持つページを更新するときは要注意。** 全文置換の中に既存の子ページへの参照を含めないと
「子ページを削除しようとしている」400エラーになる。Markdownリンクや素のURLでは参照と認識されないため、
次のタグで含める。

```
<page url="https://app.notion.com/p/<ハイフン無しページID>">ページタイトル</page>
```

子ページごと消したいときだけ `--allow-deleting-content` を付ける。

## 判断が要る部分

- **Notion vs リポジトリ。** 技術決定はADR（`docs/adr/`）、プロダクト仕様はNotion。
- **要件定義⇔基本設計の対応付け。** 基本設計の機能設計ページには、対応する要件ID（`REQ-XXX`）を書く。
  切れると、要件が変わったときに基本設計側の見直しが漏れる。
