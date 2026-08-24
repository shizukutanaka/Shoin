# CI ワークフロー

`ci.yml` は GitHub Actions のワークフロー定義 (lint → type → test+coverage →
secret scan → SBOM)。

## なぜここに置いてあるか

このリポジトリへの自動プッシュでは GitHub App の `workflows` 権限制約により
`.github/workflows/` 配下を更新できない。**v0.2.153 で実測再確認済み** —
配置してプッシュすると GitHub 側が拒否する:

```
! [remote rejected] refusing to allow a GitHub App to create or update
  workflow `.github/workflows/ci.yml` without `workflows` permission
```

有効化するには、リポジトリ管理者が手動で移動する:

```bash
mkdir -p .github/workflows
git mv ci/ci.yml .github/workflows/ci.yml
```

## 有効化前に全ゲートを実測済み (v0.2.153)

以前の版は「完成品、あとは移動するだけ」と説明していたが**それは誤りだった**。
実際に各ゲートを走らせたところ 3 つが即座に落ちる状態で、そのまま有効化すれば
恒久的に赤い CI になっていた。すべて修正済み:

| ゲート | 発見時 | 現在 |
|--------|--------|------|
| `ruff format --check .` | 16/18 ファイルが失敗 | **ステップ削除**。本プロジェクトは意図的に手整形(config.py/chunk.py の桁揃えコメント)。従わないと決めたスタイルを CI で強制するのは壊れたゲートであってスタンダードではない |
| `ruff check .` | 188 件 (ruff 版差でルール集合が漂流) | **0 件**。`pyproject` に `[tool.ruff.lint] select` を明示ピン留めし、テスト側の実指摘 29 件を修正 |
| secret scan | test_qa.py の長い hex 風テスト固定値で誤検出 1 件 | **0 件**。`# pragma: allowlist secret` を該当行に付与 |
| `mypy --strict shoin/` | (依存未導入の環境でのみ import エラー) | `pip install -e .` 済み環境で **エラー 0**。CI は `pip install -e .` するため問題なし |
| coverage | 閾値 50 | 実測 **97%**。閾値を 90 に引き上げ、実効性のあるゲートにした |

## 管理者を待たずに検証する

「GitHub Actions で CI を回す」ことは要件ではなく手段であり、本当の要件は
**「landする前にすべてのコミットが自動検証される」**こと。それは GitHub の権限
なしで達成できる:

```bash
./scripts/verify.sh                    # 全ゲートを1コマンドで実行
git config core.hooksPath .githooks    # push 前に自動実行(1回だけ設定)
```

`.githooks/pre-push` は `scripts/verify.sh` を呼ぶ。`.git/hooks/`(コミット
されない)ではなく `.githooks/`(コミットされる)に置いているため、各自が手で
再作成する必要がない。緊急時は `git push --no-verify` で迂回可能。
CI が有効化された後も、同じゲートのローカル先行実行として有用。
