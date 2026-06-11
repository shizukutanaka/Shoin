# Contributing to Shoin

## 開発環境のセットアップ

```bash
git clone https://github.com/shizukutanaka/shoin && cd shoin
pip install -e . && pip install ruff mypy coverage
```

## テスト・品質ゲート (PR前に全通過)

```bash
for f in tests/test_*.py; do python3 "$f"; done
ruff format --check . && ruff check .
mypy --strict shoin/
```

## ブランチ / コミット規約

- `feature/<issue>-<説明>` / `fix/<issue>-<説明>`
- Conventional Commits (`feat:` `fix:` `docs:` `refactor:` `test:` `chore:`)
- PRは diff 500行以内。超える場合は分割

## 設計原則

- 依存追加は原則禁止 (現状 pypdf のみ)。stdlibで書けるものはstdlibで
- 全Studio/QA出力に citation_report を付与する不変条件を壊さない
- ネットワークバインドは 127.0.0.1 限定を維持
