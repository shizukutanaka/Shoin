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

## 5. 監査ラウンドの雛形（find → 敵対的検証 → 修正）

v0.2.128 で 8 件（うち HIGH 1 件）を検出・修正した実績パターン:

1. **find（発見）** — 領域別に並列で探索。直近セッションで追加したコードを最優先
   対象にする（新規コードにバグが集中する）。フロントエンドは静的解析が無いので
   別枠で必ず1本回す。
2. **発見の採用基準** — 「具体的な入力/状態 → 具体的な誤った観測可能出力（または
   クラッシュ）」を提示できるものだけ。"could theoretically..." は却下。
   スタイル・型ヒント欠落・現実の trigger が無いスケール懸念は対象外。
3. **verify（敵対的検証）** — 発見ごとに独立した検証者（3票推奨）に**反証を試みさせる**。
   よくある却下理由: 同一関数内で既にガード済み / 実呼び出し経路から到達不能 /
   CLAUDE.md で意図的設計と明記済み / 追跡すると再現しない。過半数が反証したら棄却。
   検証者は判断に迷ったら `refuted=true`（懐疑側にデフォルト）。
4. **fix** — 生き残った発見を severity 順に修正。各修正に §1 の儀式をフルで適用
   （回帰テスト + fail-then-pass、HIGH なら並行テストを数十回連続実行して確認）。
5. **記録** — Version History に検出数・確定数・各修正を1エントリで残す。

Workflow ツールが使える場合は pipeline（find→verify を item 毎に流す）で実装できるが、
明示的な opt-in（ultracode / ユーザー依頼）が無ければ Agent ツールの個別 subagent か
手動監査で足りる。

## 6. 危険地図（ファイル別・触る前に読むべき History）

- **`store.py`** — migration の並行冪等性が生命線。`ThreadingHTTPServer` がリクエスト毎に
  `Store()` を開くため同一 migration が並行実行される。`ALTER TABLE ADD COLUMN` は
  `IF NOT EXISTS` が無く duplicate-column リカバリが必要（v0.2.128）。`_retry_on_lock` は
  `"locked" not in str(exc)` の**部分一致**で判定するため、"locked" を含まない
  OperationalError（duplicate column 等）はリトライされない — この境界を意識せよ。
- **`server.py`** — `_h_ask_sse` の SSE 切断ガードは層状に積まれている
  （headers 送信前 / meta 送信 / delta ストリーム / done / 0トークン応答、
  v0.2.39/49/55/116）。1つ触ると孤立ユーザーターンが再発する。`generation_lock` は
  非再入で、1リクエストが二重取得しない設計（v0.2.128 でrewrite呼び出しをロック外へ）。
  例外ハンドラ内の二重フォルト対策は `_safe_error`（v0.2.89/90）。
- **`chunk.py`** — `estimate_tokens()` / `_truncate_tokens()`(qa.py) / `_tail()` の3関数は
  トークン計数で**同期義務**がある（`_LONG_RUN_THRESHOLD`・`_is_word_char` を共有、
  v0.2.114/115/119）。1つ変えたら3つ揃える。`_CJK_RANGES` は `is_cjk`/`query_terms`/
  `fts_query`/`_NEG_RE` が共有する単一の真実（v0.2.107/118）。
- **`search.py`** — `_NEG_RE` の lookbehind は ASCII と CJK の両方を除外する必要がある
  （CJK直後のハイフンを否定構文と誤認しない、v0.2.128）。RRF スコアは lexical rerank 前に
  `_minmax` で [0,1] 正規化必須（v0.2.60）。`rrf_fuse_lists` の Hit マージは list 順序に
  依存しない（v0.2.125）。
- **`citation.py`** — 「確信できる時のみ主張、判定不能なら沈黙」が設計原則（v0.1.4/1.5）。
  集約スコアを足したくなっても不可。`CONFIRM_MIN=0.30` は CJK 較正値。
