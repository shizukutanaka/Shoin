# Shoin 作業指示書 — Sonnet 級モデル向け

対象: Claude Sonnet 級のモデルで動くエージェントセッション。
このリポジトリは50回超の監査ラウンドを経た高密度なコードベースであり、
一見冗長に見えるコード・コメントの多くが実バグの再発防止である。
**既存の挙動を「単純化」「改善」したくなっても、本書の許可範囲外なら手を出さない。**

まず読むもの: `CLAUDE.md` のアーキテクチャ節（Version History は必要箇所のみ）→
本書 → `docs/product-review.md` の改善案バックログ（「Sonnet可」の項目が着手候補）。

## 1. 必読の儀式（省略禁止 — Opus 用と同一内容）

1. **バージョンbump三点セット** — 1つの変更セットにつき:
   - `shoin/config.py` の `VERSION`
   - `pyproject.toml` の `version`
   - `tests/test_core.py` の `test_version` のアサーション
   さらに `CLAUDE.md` の Version History 節の**先頭**にエントリを追記し、
   見出し行 `## Version History: v0.1.37 → v0.2.NNN` の終端も更新する。
   書式は直近エントリに倣う。
2. **fail-then-pass 検証** — バグ修正には必ず回帰テストを書き、
   `git stash push <修正ファイル>` で修正前コードに戻して**テストが落ちる**ことを
   確認してから `git stash pop` する。落ちないテストは回帰テストではない。
3. **UI 変更は実ブラウザで検証** — `shoin/static/index.html` を変えたら
   Playwright（chromium は `/opt/pw-browsers/chromium`）+ 実サーバーで
   操作を再現して確認する。pytest にブラウザテストは残さない。
   （自信がなければ UI 変更自体を見送り、発見として記録する。）
4. **静的検査** — 変更ファイルで `python -m ruff check` と `python -m mypy shoin/`
   をクリーンに保つ。**`ruff format` は絶対に実行しない**（手整形スタイル）。
5. **完了条件** — `python -m unittest discover -s tests` 全緑。
6. **push** — 指定作業ブランチに push し、`git push origin HEAD:main` で
   公開 `main` を追従。タグ push・`.github/workflows/` 書き込みは権限上不可。

## 2. 推奨タスク（この範囲で価値を出す）

- **再現手順が確立しているバグの修正** — まず自分の環境で再現し、
  fail-then-pass の回帰テストを付けて直す。再現できないなら直さない。
- **テスト追加** — 既存挙動のうちカバレッジの薄い箇所（エラーパス、
  エンコーディング境界、i18n 両ロケール）。
- **i18n 文字列の追加・修正** — 必ず ja/en 両方。プレースホルダ名（`{v}`/`{n}` 等）が
  呼び出し側の kwargs と両ロケールで一致していることを確認（v0.2.128 の教訓）。
- **ドキュメント同期** — 文書の主張をコードの実挙動と突合してから直す。
  「コードを読まずに文書だけ直す」ことは、このプロジェクトで繰り返し
  空約束バグ（存在しない機能の記載）を生んできた（v0.2.75/112/129）。
- **既存パターン踏襲の小さな加算的変更** — 例: 既存の `_STRINGS`/`_t()` 機構への
  文字列追加、既存 TypedDict への `NotRequired` フィールド追加と `.get()` ガード、
  CLI サブコマンドの薄いラッパ追加（store/pipeline の既存メソッドを呼ぶだけのもの）。

## 3. 禁止事項（Opus 級へ委譲 — 発見しても自分で直さない）

- `store.py` の **migration / スキーマ / FTS5 トリガ**の追加・変更
  （並行冪等性の設計が必要。壊すと既存ユーザーの DB 起動が壊れる）
- `search.py` の**検索スコアリング・融合・ランキング**（RRF 定数、MMR、
  正規化、neg-term 正規表現）の変更
- `citation.py` の**検証セマンティクス**（CONFIRM_MIN / MISMATCH_GAP、
  confirmed/misattributed/uncited の判定条件）の変更
- `qa.py` の **LLM プロンプト文言・トークン予算定数**の変更
- `server.py` の **`generation_lock` / SSE 切断処理**周りの変更
  （切断タイミング毎に異なるガードが張られており、v0.2.39/49/55/116 の
  積み重ねを一手で壊しうる）
- 新しい pip 依存の追加、`ruff format` の適用、shipped migration の編集

## 4. エスカレーション手順

禁止領域や確信の持てない箇所で問題を**発見**したら、修正せずに記録する:

- `CLAUDE.md` の Version History の自分のエントリ内に
  `**Noted (not actioned)**:` 段落として、①何を見つけたか ②なぜ問題と考えるか
  ③再現手順または該当行 を書く（v0.2.72 / v0.2.133 の前例書式）。
- 修正パッチを「提案」としてエントリに書くのは可。適用はしない。

これは怠慢ではなく、このプロジェクトの規律である。誤った修正は
未修正の既知バグより高くつく。
