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
