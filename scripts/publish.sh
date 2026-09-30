#!/usr/bin/env bash
# 原稿からHTMLを生成し、コミットしてpushし、公開ページが今回の内容になるまで待つ。
#
# 使い方:
#   bash scripts/publish.sh YYYY-MM-DD            # 生成 → コミット → push → URL表示
#   bash scripts/publish.sh YYYY-MM-DD --wait     # さらに公開ページが今回の build-id を返すまで最大5分待つ
#
# pushにはこのリポジトリへの書き込み権限（gh auth または git の認証設定）が必要。
# 終了コード: 0 成功 / 1 引数・原稿・生成の失敗（pushの失敗もgitの終了コードで非0） / 2 公開待ちの時間切れ

set -euo pipefail
cd "$(dirname "$0")/.."

WAIT_LIMIT=300  # 秒

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "使い方: bash scripts/publish.sh YYYY-MM-DD [--wait]" >&2
  exit 1
fi
date_arg="$1"
wait_flag="${2:-}"
if [[ ! "$date_arg" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "日付は YYYY-MM-DD で指定してください: $date_arg" >&2
  exit 1
fi
if [[ -n "$wait_flag" && "$wait_flag" != "--wait" ]]; then
  echo "不明なオプション: $wait_flag（使えるのは --wait だけです）" >&2
  exit 1
fi
if [[ ! -f "content/$date_arg.md" ]]; then
  echo "content/$date_arg.md がありません" >&2
  exit 1
fi

python3 scripts/build.py

article="articles/$date_arg.html"
local_id="$(sed -n 's/.*<meta name="build-id" content="\([0-9a-f]*\)">.*/\1/p' "$article" | head -n 1)"
if [[ -z "$local_id" ]]; then
  echo "$article に build-id がありません（templates/article.html を確認してください）" >&2
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  git add -A
  if git log --oneline -- "content/$date_arg.md" | grep -q .; then
    git commit -q -m "$date_arg の記事を更新"
  else
    git commit -q -m "$date_arg の記事を追加"
  fi
else
  echo "変更なし（コミットしません）"
fi

git push -q origin HEAD:main
url="$(python3 scripts/build.py --url "$date_arg")"
echo "push完了: $url (build-id $local_id)"

if [[ "$wait_flag" == "--wait" ]]; then
  echo "公開ページが今回の内容になるまで待っています（最大 ${WAIT_LIMIT} 秒）..."
  start=$SECONDS
  while (( SECONDS - start < WAIT_LIMIT )); do
    remaining=$(( WAIT_LIMIT - (SECONDS - start) ))
    timeout=$(( remaining < 20 ? remaining : 20 ))
    body="$(curl -s -L --max-time "$timeout" "$url?t=$(date +%s)" || true)"
    if [[ "$body" == *"content=\"$local_id\""* ]]; then
      echo "公開確認: $url"
      exit 0
    fi
    (( SECONDS - start + 10 < WAIT_LIMIT )) || break
    sleep 10
  done
  echo "${WAIT_LIMIT}秒待っても公開ページが今回の内容（build-id $local_id）になりません。GitHub Pagesの設定とデプロイ状況を確認してください: $url" >&2
  exit 2
fi
