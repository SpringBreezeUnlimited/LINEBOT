テスト手順 (ローカル実行)

前提:
- Node.js と npm がインストールされていること
- Docker等の仮想ブラウザは不要。ローカルで Chromium を起動して検証します

1) 依存パッケージのインストール

```bash
npm install playwright
# あるいはプロジェクトで playwright が既にある場合は省略
```

2) Playwright テストの実行

```bash
node tests/playwright_test.js
```

説明:
- スクリプトはワークスペースのルートで `python3 -m http.server 8000` を起動し、`/test_qr.html` を配信します。
- Playwright は Chromium をヘッドレスで起動し、`--use-fake-ui-for-media-stream` と `--use-fake-device-for-media-stream` を使ってカメラ権限と仮想カメラを自動で許可します。
- 成功条件: ページ内の `#qr-video` の `srcObject` にカメラストリームのトラックが存在すること。

ローカル実機でブラウザの実際のカメラ（物理カメラ）を使って手動確認する場合は、以下の手順:

1. サーバーを起動

```bash
# Python がある場合
python3 -m http.server 8000
```

2. ブラウザで https://localhost:8000/test_qr.html を開く（localhost は http でも可）
3. 「カメラ開始」ボタンを押し、ブラウザの権限要求ダイアログで許可する

問題が続く場合:
- ブラウザが `localhost` をセキュアコンテキストと認識しているか確認
- 他のアプリでカメラが使用中でないか確認
- ブラウザのサイト設定でカメラがブロックされていないか確認
