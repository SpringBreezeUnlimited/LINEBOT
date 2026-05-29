Playwright 自動操作スクリプト

概要
- `tests/playwright/test_app_operation.py` はローカルアプリ（http://localhost:8080）に対して起動確認、ログイン、ログアウトを行う簡易スクリプトです。

前提
- `.env` に `APP_USERNAME` と `APP_PASSWORD` を設定してください。
- 開発用依存をインストールしてください（下記参照）。

セットアップ
```
python -m pip install -r requirements-dev.txt
playwright install
```

実行
```
python tests/playwright/test_app_operation.py
```

注意
- このリポジトリのアプリ起動手順は Windows PowerShell 想定のため、自動で起動する機能は含めていません。アプリが起動していない場合は `SKILL.md` の手順に従って起動してください。
