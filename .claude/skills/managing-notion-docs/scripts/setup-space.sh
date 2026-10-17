#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <parent-page-id-or-url>" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATES_DIR="$(dirname "$SCRIPT_DIR")/templates"

parent_id="$(echo "$1" | grep -oE '[0-9a-f]{32}' | tail -1 || true)"
if [ -z "$parent_id" ]; then
  parent_id="$1"
fi

create_titled_page() {
  local parent="$1" title="$2" id
  id="$(ntn api v1/pages -X POST \
    parent[page_id]="$parent" \
    properties[title][title][0][text][content]="$title" \
    < /dev/null | jq -r '.id // empty')"
  if [ -z "$id" ]; then
    echo "failed to create page: $title (parent: $parent)" >&2
    exit 1
  fi
  echo "$id"
}

fill_body() {
  ntn pages edit "$1" --content "$2" < /dev/null >/dev/null
}

page_link_tag() {
  echo "<page url=\"https://app.notion.com/p/${1//-/}\">$2</page>"
}

extract_title() {
  grep -m1 '^# ' "$1" | sed 's/^# //'
}

setup_tree() {
  local files=("$1"/*.md)
  local root_file="${files[0]}"

  root_title="$(extract_title "$root_file")"
  root_id="$(create_titled_page "$index_id" "$root_title")"
  fill_body "$root_id" "$(cat "$root_file")"
  echo "root ($root_title): $root_id"

  local leaf_file leaf_title leaf_id
  for leaf_file in "${files[@]:1}"; do
    leaf_title="$(extract_title "$leaf_file")"
    leaf_id="$(create_titled_page "$root_id" "$leaf_title")"
    fill_body "$leaf_id" "$(cat "$leaf_file")"
    echo "  leaf ($leaf_title): $leaf_id"
  done
}

echo "== index page =="
index_id="$(create_titled_page "$parent_id" "$(extract_title "$TEMPLATES_DIR/index.md")")"
echo "index: $index_id"

index_body="$(cat "$TEMPLATES_DIR/index.md")"
for tree_dir in "$TEMPLATES_DIR"/*/; do
  echo "== $(basename "$tree_dir") =="
  setup_tree "$tree_dir"
  index_body="$index_body

## $root_title

$(page_link_tag "$root_id" "$root_title")"
done

echo "== updating index links =="
fill_body "$index_id" "$index_body"

echo
echo "done."
echo "index page id: $index_id"
