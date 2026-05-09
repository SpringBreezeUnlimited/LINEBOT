# LINEBOT

## Setup
1. Copy `.env.example` values into your deployment environment.
2. Generate `ADMIN_PASSWORD_HASH` with Werkzeug `generate_password_hash`.
3. If you want a separate audit-only administrator, also set `AUDIT_ADMIN_PASSWORD_HASH`.
4. Run app with `gunicorn main:app` (see `Procfile`).

## Render Deployment
1. Create a new Web Service on [Render.com](https://render.com)
2. Set the following environment variables in Render dashboard:
   - `ALLOWED_HOSTS`: Your Render app domain (e.g., `myapp.onrender.com`). For multiple domains, use comma-separated values.
   - Other required vars: `SECRET_KEY`, `ADMIN_PASSWORD_HASH`, `AUDIT_ADMIN_PASSWORD_HASH`, `CHANNEL_ACCESS_TOKEN`, `CHANNEL_SECRET`, `DATABASE_URL`, `OWNER_LINE_ID`
3. Deploy. Render automatically detects `Procfile` and runs with Gunicorn.
   - ALLOWED_HOSTS must be set; the app will fail to start without it in production.

## Batch Call Queue
1. Set `BATCH_CALL_RUNNER_TOKEN` in your app environment.
2. In GitHub repository secrets, add:
   - `BATCH_CALL_RUNNER_TOKEN`: same value as the app env var
   - `CALL_QUEUE_TASK_URL`: `https://your-app.example.com/tasks/process-call-queue`
3. The workflow `.github/workflows/process-call-queue.yml` runs every 1 minute and can also be triggered manually from GitHub Actions.

## Security
- Security hardening summary and operational checklist: `SECURITY_HARDENING.md`

---

## 日本語

### セットアップ
1. `.env.example` の値をデプロイ先の環境変数に設定してください。
2. Werkzeug の `generate_password_hash` で `ADMIN_PASSWORD_HASH` を生成してください。
3. 監査専用の管理者を分けたい場合は `AUDIT_ADMIN_PASSWORD_HASH` も設定してください。
4. `gunicorn main:app` でアプリを起動します（`Procfile` 参照）。

### Render へのデプロイ
1. [Render.com](https://render.com) で新しい Web Service を作成します。
2. Render のダッシュボードで以下の環境変数を設定します。
   - `ALLOWED_HOSTS`: Render のアプリドメイン（例: `myapp.onrender.com`）。複数ドメインはカンマ区切りで指定します。
   - その他の必須変数: `SECRET_KEY`, `ADMIN_PASSWORD_HASH`, `AUDIT_ADMIN_PASSWORD_HASH`, `CHANNEL_ACCESS_TOKEN`, `CHANNEL_SECRET`, `DATABASE_URL`, `OWNER_LINE_ID`
3. デプロイします。Render は `Procfile` を自動検出して Gunicorn で起動します。
   - 本番では `ALLOWED_HOSTS` の設定が必須です。未設定だとアプリは起動に失敗します。

### バッチ呼び出しキュー
1. アプリ環境変数に `BATCH_CALL_RUNNER_TOKEN` を設定します。
2. GitHub リポジトリの secrets に以下を追加します。
   - `BATCH_CALL_RUNNER_TOKEN`: アプリ環境変数と同じ値
   - `CALL_QUEUE_TASK_URL`: `https://your-app.example.com/tasks/process-call-queue`
3. ワークフロー `.github/workflows/process-call-queue.yml` は 1 分ごとに実行され、GitHub Actions から手動実行も可能です。

### セキュリティ
- セキュリティ強化の概要と運用チェックリスト: `SECURITY_HARDENING.md`
