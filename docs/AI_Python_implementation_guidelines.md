# AIエンジニアが気をつけたい Python 実装ノウハウ（要約）

この記事は、AIエンジニアが実務で守るべき Python 実装のコツをレベル別にまとめたものです。

主なポイント

- 命名規則: PEP8 準拠。冗長な接頭辞を避け、reverse notation や品詞を意識して意図が伝わる名前にする。
- import の順序: 標準ライブラリ → サードパーティ → 自作モジュール（各グループを空行で区切る）。
- 再現性: 乱数シードを固定（`os.environ['PYTHONHASHSEED']`, `random.seed`, `np.random.seed`, `torch.manual_seed` 等）。
- 関数化と単一責務: 長いスクリプトを分割し、SOLID（特に S: Single Responsibility）を意識する。
- 型ヒントと docstring: 関数・クラスに型ヒントと docstring を付ける。
- モデル保存: 学習済みモデルを保存する際は前処理パイプラインやハイパーパラメータも併せて保存する。
- 例外処理とログ: try 範囲を狭くし、想定される例外ごとのハンドリングと `logging` を導入する。
- 引数設計: 原則引数は 3 つ以下。必要なら辞書や `**kwargs` を用いる。
- チーム開発: フォーマッタを統一（例: `black`）、PR テンプレを用意してチェックリスト化する。

リポジトリへの適用案

- リポジトリ直下に PR テンプレを追加し、記事のチェックリストを反映する。
- `.vscode/settings.json` でフォーマッタを統一する（例: `black`）。
- 主要関数に型ヒント・docstring を徐々に追加する。

（詳細は元記事: https://qiita.com/sugulu_Ogawa_ISID/items/c0e8a5e6b177bfe05e99 ）
