# Shoin 作業指示書 — Opus 級モデル向け

対象: Claude Opus 級（またはそれ以上）のモデルで動くエージェントセッション。
このリポジトリで**自律的に**設計・実装・監査まで行ってよい。判断に迷う点が
なければユーザー確認を待たずに進めてよいが、下記の儀式と設計原則は絶対。

まず読むもの: `CLAUDE.md`（アーキテクチャ・Version History）→ 本書 →
`docs/product-review.md` の改善案バックログ（着手候補の台帳）。

## 1. 必読の儀式（省略禁止）

1. **バージョンbump三点セット** — 1つの変更セットにつき:
   - `shoin/config.py` の `VERSION`
   - `pyproject.toml` の `version`
   - `tests/test_core.py` の `test_version` のアサーション
   さらに `CLAUDE.md` の Version History 節の**先頭**にエントリを追記し、
   見出し行 `## Version History: v0.1.37 → v0.2.NNN` の終端も更新する。
   エントリの書式は直近のエントリ群に倣う（何を・なぜ・再現・修正・テスト・
   検証コマンド結果まで1エントリで完結させる）。
2. **fail-then-pass 検証** — バグ修正には必ず回帰テストを書き、
   `git stash push <修正ファイル>` で修正前コードに戻して**テストが落ちる**ことを
   確認してから `git stash pop` する。落ちないテストは回帰テストではない。
3. **UI 変更は実ブラウザで検証** — `shoin/static/index.html` を変えたら
   Playwright（chromium は `/opt/pw-browsers/chromium`）+ 実サーバーで
   操作シーケンスを再現し、修正前に壊れ・修正後に直ることを両方確認する。
   pytest にブラウザテストは**残さない**（このプロジェクトの検証規約。
   v0.2.88/109/122/128 の前例に従い、検証内容は Version History に記録する）。
4. **静的検査** — 変更したソースファイルで `python -m ruff check` と
   `python -m mypy shoin/` をクリーンに保つ（既存の pypdf スタブ注記のみ許容）。
   **`ruff format` は絶対に実行しない** — このコードベースは手整形スタイルで、
   自動整形は16ファイルを書き換えてしまう（v0.2.133 の記録参照）。
5. **完了条件** — `python -m unittest discover -s tests` 全緑。
   件数が増えたら Version History エントリに新しい総数を記す。
6. **push** — 指定作業ブランチに push し、`git push origin HEAD:main` で
   公開 `main` を追従させる（両ブランチとも同一コミットを指すのが正常状態）。
   タグ push・`.github/workflows/` への書き込みは権限上不可能（試行不要）。

## 2. 設計原則（違反 PR は書かない）

- **依存ゼロ**: ランタイム依存は stdlib + pypdf のみ。新しい pip 依存を足さない。
- **Lightweight First**: レイテンシ・メモリを増やす機能はデフォルト OFF にし、
  `SHOIN_*` 環境変数で opt-in（前例: `SHOIN_MULTI_QUERY`）。README の設定表に載せる。
- **graceful degradation**: LLM 到達不能・埋め込み不能は `LLMError` を黙って
  キャッチし機能縮退（例外を UI/CLI に漏らさない）。ただし DB ロック等の
  別クラスの障害を同じ握り潰しに巻き込まない（v0.2.82 の教訓）。
- **migration は追記専用・並行冪等** — `MIGRATIONS` は末尾追加のみ。
  `ThreadingHTTPServer` がリクエスト毎に `Store()` を開くため、同一 migration が
  並行実行されても壊れないこと（`IF NOT EXISTS`、`ALTER TABLE ADD COLUMN` の
  duplicate-column リカバリは v0.2.128 の実装を踏襲）。検証は該当並行テストを
  数十回連続実行して確認する。
- **引用検証は「確信できる時のみ主張」** — 字句シグナルの非対称性
  （高重なり=確実 / 低重なり=判定不能）を崩す変更、集約スコアの導入は不可。
- **チャンク本文の純粋性** — `chunks.text` と `source_bodies` は純粋な原文のまま。
  検索用メタデータ（context 等）を本文・LLMプロンプト・引用検証に混ぜない。
- **i18n** — ユーザー向け文字列は各モジュールの `_STRINGS` に ja/en 両方を追加。

## 3. Opus に開放される作業領域

以下は判断の難しい領域であり、Opus 級のみが自律的に触ってよい
（Sonnet 級の指示書ではこれらを禁止している）:

- スキーマ migration の追加（store.py）と FTS5 トリガ設計
- `search.py` の検索スコアリング・融合・ランキングの変更
- `citation.py` の検証セマンティクス（CONFIRM_MIN / MISMATCH_GAP 等）の変更
- `qa.py` の LLM プロンプト文言・トークン予算配分の変更
- `generation_lock` 等の並行性・ロック設計
- マルチエージェント監査ラウンド（find → 3票敵対的検証 → 修正。v0.2.128 前例。
  発見は「具体的な入力と誤った出力を提示できるもの」のみ採用し、検証者には
  反証を試みさせる）

## 4. 着手先

`docs/product-review.md` の改善案バックログから「担当推奨: Opus」の項目、
または新規監査ラウンド。ユーザーから明示の指示があればそれを最優先。
