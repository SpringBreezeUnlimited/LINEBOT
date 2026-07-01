# LINEBOT 
　
## セットアップ
1. `.env.example` の値をデプロイ先の環境変数に設定してください。
2. Werkzeug の `generate_password_hash` で `ADMIN_PASSWORD_HASH` を生成してください。
3. 監査専用の管理者を分けたい場合は `AUDIT_ADMIN_PASSWORD_HASH` も設定してください。
4. `gunicorn main:app` でアプリを起動します（`Procfile` 参照）。

## Render へのデプロイ
1. [Render.com](https://render.com) で新しい Web Service を作成します。
2. Render のダッシュボードで以下の環境変数を設定します。
   - `ALLOWED_HOSTS`: Render のアプリドメイン（例: `myapp.onrender.com`）。複数ドメインはカンマまたは空白区切りで指定できます。
   - その他の必須変数: `SECRET_KEY`, `ADMIN_PASSWORD_HASH`, `AUDIT_ADMIN_PASSWORD_HASH`, `CHANNEL_ACCESS_TOKEN`, `CHANNEL_SECRET`, `DATABASE_URL`, `OWNER_LINE_ID`
3. デプロイします。Render は `Procfile` を自動検出して Gunicorn で起動します。
   - 本番では `ALLOWED_HOSTS` の設定が必須です。未設定だとアプリは起動に失敗します。

## バッチ呼び出しキュー
1. アプリ環境変数に `BATCH_CALL_RUNNER_TOKEN` を設定します。
2. GitHub リポジトリの secrets に以下を追加します。
   - `BATCH_CALL_RUNNER_TOKEN`: アプリ環境変数と同じ値
   - `CALL_QUEUE_TASK_URL`: `https://your-app.example.com/tasks/process-call-queue`
3. ワークフロー `.github/workflows/process-call-queue.yml` は 1 分ごとに実行され、GitHub Actions から手動実行も可能です。
4. 毎日 0:00 JST に待機中・呼出中の予約は自動でキャンセルされます。この深夜キャンセルではユーザー通知は送りません。

## セキュリティ
- セキュリティ強化の概要と運用チェックリスト: `SECURITY_HARDENING.md`
