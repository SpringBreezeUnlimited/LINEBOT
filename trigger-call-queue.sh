#!/bin/bash

# 外部サービスからの定期実行用スクリプト
# 例: cron-job.org や similar service から呼び出す

# 環境変数の設定（実際の値に置き換え）
CALL_QUEUE_TASK_URL="${CALL_QUEUE_TASK_URL:-https://your-app.example.com/tasks/process-call-queue}"
BATCH_CALL_RUNNER_TOKEN="${BATCH_CALL_RUNNER_TOKEN:-your-token-here}"

# 実行
curl --fail --show-error --silent \
  -X POST "$CALL_QUEUE_TASK_URL" \
  -H "Authorization: Bearer $BATCH_CALL_RUNNER_TOKEN"

echo "Call queue task triggered at $(date)"