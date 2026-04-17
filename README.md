# LINEBOT

## Setup
1. Copy `.env.example` values into your deployment environment.
2. Generate `ADMIN_PASSWORD_HASH` with Werkzeug `generate_password_hash`.
3. If you want a separate audit-only administrator, also set `AUDIT_ADMIN_PASSWORD_HASH`.
4. Run app with `gunicorn main:app` (see `Procfile`).

## Batch Call Queue
1. Set `BATCH_CALL_RUNNER_TOKEN` in your app environment.
2. In GitHub repository secrets, add:
   - `BATCH_CALL_RUNNER_TOKEN`: same value as the app env var
   - `CALL_QUEUE_TASK_URL`: `https://your-app.example.com/tasks/process-call-queue`
3. The workflow `.github/workflows/process-call-queue.yml` runs every 1 minute and can also be triggered manually from GitHub Actions.

## Security
- Security hardening summary and operational checklist: `SECURITY_HARDENING.md`
