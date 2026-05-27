# FlexMessage 移行手順

このドキュメントは `TextMessage` ベースの返信をLINEのFlexMessageへ移行する手順をまとめたものです。

1. 目的
   - ユーザー通知を見やすく、操作しやすいFlexレイアウトに置換する。

2. 実装済み
   - `flex_templates.py` に主要テンプレート（受付完了、呼出、待ち時間、キャンセル、自動キャンセル）を追加。
   - `main.py` の送信関数をFlex辞書を受け取れるよう拡張し、TextMessageへのフォールバックを実装。

3. 残作業（手動）
   - 画像・アセットの用意: `static/img/` にロゴやアイコンを配置。
   - ボタンアクションの最適化: Postback と message の使い分けを確認。
   - ステージング検証: ステージング環境へデプロイしてWebhook経由で表示を確認。
   - 本番ロールアウト計画: カナリアや段階的リリースを検討。

4. テスト
   - 単体でFlexテンプレートの出力構造を検証するテストを追加済み。

5. ローカルでの確認方法
   - 環境変数を設定（`SECRET_KEY`, `CHANNEL_ACCESS_TOKEN`, `CHANNEL_SECRET`, `DATABASE_URL` など）。
   - サーバを起動して、LINE Developers のWebhook送信でメッセージ処理をテスト。
