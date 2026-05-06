---
name: app-operation-playwright
description: Playwright（Playwright MCP）でローカル Web アプリ（http://localhost:8080）を確実に起動・ログイン・操作・ログアウトする手順
---

# App Operation via Playwright（画面操作手順）

## この SKILL の前提（重要）
- 対象URL：`http://localhost:8080`
- アプリ起動は **Windows PowerShell** を前提とします（コマンドも PowerShell を記載）。
- DB は **docker-compose.yml が存在** し、MySQL（`todo-chat` / `todo-chat-test`）を起動します。
  - ただし **テスト用コンテナ（todo-chat-test）の health 待機は不要** です（要件として不要）。
- アプリ起動コマンドは以下です。
  - `cd .\webapp\`
  - `.\mvnw.cmd spring-boot:run`
- Java のバージョン指定は以下を **必ず実行** します（JDK 21 固定）。
  - `$env:JAVA_HOME="C:\Program Files\Java\jdk-21"`
  - `$env:Path="$env:JAVA_HOME\bin;$env:Path"`

## 目的
- Copilot が Playwright（Playwright MCP）を用いて、ローカルアプリを
  **起動状態確認 → 必要なら起動 → ログイン → 画面操作 → ログアウト**
  まで確実に実施できるようにします。

## 禁止事項
- **資格情報（ID/Password）の推測は禁止**（見つからない場合は止めてユーザー確認）
- **破壊的操作は禁止**（削除、退会、課金、管理者操作、データ変更など）

## Playwright の要素特定ルール（本アプリ前提）
本アプリの HTML には `data-testid` 等のテスト用属性が存在しないため、以下の順で特定します。

1. **ラベルと紐づく入力**（`label` → `input`）
2. **placeholder**（入力欄のヒント文字）
3. **name / id** 属性（フォーム部品が持っている場合）
5. **ボタンやリンクの表示テキスト**（例：`Login` / `ログアウト`）

`nth-child` や過度に長い CSS セレクタは使いません

---

# 作業手順

## 1. 事前に「起動済みか」を確認する（確実チェック）

### 1.1 HTTP 疎通確認（最優先）
**HTTP 200/3xx が返るなら起動済み**です。

```powershell
try {
  $r = Invoke-WebRequest -UseBasicParsing http://localhost:8080/ -TimeoutSec 3
  $r.StatusCode
} catch {
  "NOT_RUNNING"
}
```

* 結果が `NOT_RUNNING` の場合 → 「2. 起動手順」へ

### 1.2 8080 LISTEN 確認（補助）

```powershell
netstat -ano | Select-String ":8080"
```

### 1.3 docker-compose のコンテナ起動確認（補助：DB 起動判定）

`docker-compose.yml` は DB 用です。

```powershell
docker compose -f .\docker-compose.yml ps
```

---

## 2. 起動手順（未起動の場合）

### 2.1 前提：Java 21 を必ず指定する

```powershell
$env:JAVA_HOME="C:\Program Files\Java\jdk-21"
$env:Path="$env:JAVA_HOME\bin;$env:Path"
java -version
```

* `java -version` の出力に **21** が含まれることを確認します。

### 2.2 DB（MySQL）を docker compose で起動する（確定手順）

#### 2.2.1 docker compose 実行（DB 起動）

```powershell
docker compose -f .\docker-compose.yml up -d
docker compose -f .\docker-compose.yml ps
```

#### 2.2.2 テスト用コンテナ（todo-chat-test）について

* `todo-chat-test` はテスト用のため **health 待機は実施しません**。
* 本番相当の DB として `todo-chat` が起動していることを `docker compose ps` で確認できれば十分です。

### 2.3 アプリ（Web）を起動する（確定コマンド）

```powershell
cd .\webapp\
.\mvnw.cmd spring-boot:run
```

### 2.4 起動完了（HTTP 応答）を確認する（確定待機）

アプリ起動後、`http://localhost:8080/` が応答するまで待機します。

```powershell
for ($i=0; $i -lt 120; $i++) {
  try {
    $r = Invoke-WebRequest -UseBasicParsing http://localhost:8080/ -TimeoutSec 2
    if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 400) { "OK: app is up"; break }
  } catch {}
  Start-Sleep -Seconds 1
}
```

---

## 3. ログイン手順（Playwright：確実フロー）

### 3.1 ログイン要否の判定（確実条件）

1. Playwright で `http://localhost:8080` を開く
2. 以下のどちらかで「ログイン画面」を断定する

   * URL パスが `http://localhost:8080/login` と一致する

* ログイン画面 → 3.2 へ
* それ以外 → ログイン済みとして 4 へ

### 3.2 ログイン実行（資格情報は .env から取得）

#### 3.2.1 資格情報（確定情報）

資格情報は `.env` の以下キーから取得します（確定）。

| 資格情報 | ソース  | キー           |
| -------- | ------ | -------------- |
| username | `.env` | `APP_USERNAME` |
| password | `.env` | `APP_PASSWORD` |

#### 3.2.2 UI 操作（要素特定は REPLACE_ME を使用）

1. `#username` でユーザー名入力欄を特定し、username を入力
2. `#password` でパスワード入力欄を特定し、password を入力
3. ボタン表示が `Login` の要素をクリック
4. 成功判定（必須）：
   * URL パスが `http://localhost:8080/home` 以外になった、または
   * ログアウト導線（`Logout`）が表示された

### 3.3 ログイン失敗時（確実対応）

* クリック後もログイン画面のままの場合：

  1. 画面上のエラーメッセージを取得して記録する（スクリーンショットも取得）
  2. `.env` の `APP_USERNAME` / `APP_PASSWORD` が正しいかをユーザーに確認する
  3. CAPTCHA/MFA/SSO の表示がある場合は、自動化不可としてユーザーに指示を仰ぐ

---

## 4. 目的の画面操作（任意）

* 操作対象・成功条件（URL 変化 / 要素表示 / トースト表示など）を **必ず定義してから** 実行します。

---

## 5. ログアウト手順（Playwright：確実フロー）

### 5.1 ログアウト導線の特定

* 表示テキストが `Logout` の要素をクリックします。

### 5.2 成功判定（必須）

以下で判定します。

- URL パスが `http://localhost:8080/login` になった

---

## 6. トラブルシューティング（確実に切り分ける）

### 6.1 `http://localhost:8080` に繋がらない

1. 1.1 の HTTP 確認を再実行
2. 2.2 で DB が起動しているか確認（`docker compose ps`）
3. 2.3 のアプリ起動（`.\mvnw.cmd spring-boot:run`）が生きているか確認（起動ターミナルのログを確認）
4. 8080 を別プロセスが占有していないか確認（1.2）

### 6.2 docker compose が立ち上がらない

* `docker compose -f .\docker-compose.yml logs --tail=200` を確認し、環境変数（`MYSQL_*`, `TZ`）が解決できているかを確認

| 資格情報 | ソース  | キー           |
| -------- | ------ | -------------- |
| MYSQL_ROOT_PASSWORD | `.env` | `MYSQL_ROOT_PASSWORD` |
| MYSQL_DATABASE | `.env` | `MYSQL_DATABASE` |
| MYSQL_USER | `.env` | `MYSQL_USER` |
| MYSQL_PASSWORD | `.env` | `MYSQL_PASSWORD` |

### 6.3 UI 要素が見つからない / クリックできない

* スクリーンショットを取得して「実際に表示されている文言 / ラベル / placeholder / name / id / aria-label」を確認します。
* 待機が必要な場合は、**待機条件（要素の表示/非表示）を明示して** 待機します。

---

## 7. 実行チェックリスト（完了条件）

* [ ] Java 21 が有効（`java -version` に 21）
* [ ] `docker compose -f .\docker-compose.yml ps` で `todo-chat` が `running`
* [ ] `http://localhost:8080/` が 200/3xx 応答
* [ ] ログイン成功判定を満たした（URL or ログアウト導線表示）
* [ ] ログアウト成功判定を満たした（URL or ログイン画面識別要素表示）
