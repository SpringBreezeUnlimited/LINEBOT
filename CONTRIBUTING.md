# 貢献ガイド

このリポジトリでは下記の開発ルールを推奨します。

- コードフォーマット: `black` を使用してください（`pyproject.toml` 設定を参照）。
- import の順序: 標準 → サードパーティ → 自作（`isort` を利用）。
- PR 作成時: 自動で実行される `pre-commit` フックを用いてコード整形・静的チェックを実行してください。
- 再現性: 実験やテストで乱数を使用する場合は `utils/repro.py` の `set_seed(seed)` を利用してシードを固定してください。
- ドキュメント: 主要関数・クラスには docstring を追加してください。

ローカルで開発する際の簡単な手順:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pre-commit install
pre-commit run --all-files
```
