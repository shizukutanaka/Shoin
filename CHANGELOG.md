# Changelog

## [Unreleased]

## [v0.1.11] - 2026-06-13
### Added
- `Store.get_source(source_id)` — 単一ソースを ID で取得するメソッドを追加。`SOURCE_NOT_FOUND` を送出するため、404 マッピングが自動適用される。

### Changed
- `Store.delete_source`: 内部の存在チェック SELECT を `get_source()` 呼び出しに置換し、ロジックの重複を解消。
- `server._h_src_text`: `store.conn.execute` による直接 SQL を `store.get_source()` に置換。Store 抽象を経由することで SQL が一か所に集約された。
- `qa.build_context`: `store.conn.execute` による直接 SQL をソース取得の `try/except StoreError` パターンに置換。ソースが削除されていても `"source-{id}"` にフォールバックする安全な挙動を維持。

## [v0.1.10] - 2026-06-13
### Fixed
- **`export_bibtex` / `export_ris` が存在しないノートブックで空文字列を返していた**: `export_markdown` は先頭で `store.get_notebook()` を呼び出して存在確認しているが、`bibtex` / `ris` 系はそれをせず空結果を 200 OK で返していた。API の `StoreError("NOTEBOOK_NOT_FOUND")` → 404 変換に引っかからないため、削除済みノートブックへのエクスポートが無音で空ファイルになっていた。各フォーマット関数の先頭に `store.get_notebook()` を追加し挙動を統一。
- **`add_source` の TOCTOU 競合でスレッドが未捕捉 `IntegrityError` を送出することがあった**: 重複チェック (`SELECT`) と挿入 (`INSERT`) の間に別スレッドが同一ソースを挿入すると `sqlite3.IntegrityError` が伝播して 500 応答になっていた。`IntegrityError` を捕捉して `StoreError("SOURCE_ALREADY_EXISTS")` に変換するよう修正。

## [v0.1.9] - 2026-06-13
### Fixed
- **`fetch_url` が旧バージョン番号の User-Agent を送信していた**: `"shoin/0.1"` とハードコードされていた User-Agent ヘッダを `f"shoin/{VERSION}"` に変更。バージョンを上げるたびに手動更新が必要な不一致を解消。
- **PDF をコンテントタイプ非依存で検出**: `extract_url` が PDF を MIME タイプ (`application/pdf`) のみで判定していたため、`Content-Type: application/octet-stream` で配信されるサーバから取得した PDF が HTML/テキストとして誤処理されていた。マジックバイト (`%PDF`) によるフォールバック検出を追加。
- **`delete_source` / `delete_note` が存在しない ID で無音成功していた**: 各削除メソッドに存在チェックを追加し、不明 ID に対して `SOURCE_NOT_FOUND` / `NOTE_NOT_FOUND` エラーを送出するよう変更。API の 404 処理がストア層でも一貫して適用されるようになった。

## [v0.1.8] - 2026-06-13
### Added
- **Markdown エクスポートにチャット履歴を追加**: `shoin export <id>` の出力に `## チャット履歴` セクションを追加。各 Q&A ターンと、アシスタント発言に対応する `source_map`(引用元タイトルの対応表)を含む。ノートブックの研究会話を丸ごとアーカイブできるようになった。
- **CLI が引用の根拠検証結果を表示**: `_print_report` が `confirmed` (✓根拠確認済み) と `misattributed` (⚠番号取り違えの可能性) マーカーを出力するよう拡張。「三段の引用検証」を宣言しながら CLI には一段目(範囲チェック)しか表示されていなかった矛盾を解消。

## [v0.1.7] - 2026-06-13
### Added
- **CLI parity (REQ-103)**: `notebook rename <id> <name>` と `messages clear <notebook_id>` サブコマンドを追加。v0.1.2 でストア・API・UIに実装した改名とチャットクリアが CLI から到達不能だった。「CLIが全コア機能を露出する」という REQ-103 に違反していた。
- `Store.list_messages_recent(notebook_id, limit)` — SQL `ORDER BY id DESC LIMIT ?` で末尾 N 件を直接取得。`list_messages()[-limit:]` はメッセージ件数分を全ロードしていたため、長期ノートブックで無駄なメモリ/IO が発生していた。

### Fixed
- `adaptive_alpha` が `か。` で終わる質問を natural-language question として認識しなかった(ベクトル重みが加算されなかった)。`suggest_questions` の第6ラウンド修正と同根: 末尾句読点を除去してから判定するよう変更。

### Changed
- Studio `overview_hits`: 先頭 N チャンク固定から**等間隔サンプリング**に変更。100 ページ PDF のブリーフィング/年表/マインドマップが序文しか参照しなかった問題を解消。per_source=3 の場合、先頭・中間・末尾チャンクを取得するため長文ソースの全体像が Studio 出力に反映される。

## [v0.1.6] - 2026-06-13
### Fixed
- **SSE 切断による会話履歴の破壊**: クライアントが SSE ストリーム中に切断(`BrokenPipeError`)すると、ユーザーメッセージが DB に残りアシスタントメッセージが保存されないまま終了していた。次回の質問で `history_messages` がこの孤立ユーザーメッセージを含むプロンプトを構築し、LLM に連続する `user→user` メッセージを渡してしまう問題(ソクラテス問答 第6ラウンド)。対応二段階:
  1. `_h_ask_sse`: `BrokenPipeError` をキャッチして部分生成内容でもアシスタントメッセージを保存(接続切断後もDB一貫性を維持)
  2. `history_messages`: 末尾のユーザーターンをトリムする防御処理を追加(アシスタント返信のないユーザーメッセージは孤立ターンとして除外)
- **`suggest_questions` が `か。` で終わる質問を誤って除外**: `endswith("か")` チェックが日本語の文末記号(`。`)の付いた質問を除外していた。軽量 LLM は「装飾なし」指示に従わず `。` を付ける場合があり、有効な質問が無音で失われていた。末尾の句読点を除いてから `endswith("か")` を判定するよう修正。

## [v0.1.5] - 2026-06-13
### Changed
- `grounding` スコアを `CitationReport` と `verify_grounding` から削除 (ソクラテス問答 第5ラウンド)。v0.1.4 で「判定不能な場合は沈黙する」と定めたにもかかわらず、全引用が同義語言い換え(判定不能)の場合に `grounding=0.0` を出力していた。これは「グラウンディングなし」という**負の断言**であり、v0.1.4 が明示的に回避すると決めた主張そのものだった。`confirmed` と `misattributed` のリストが完全で正直なシグナルであり、集約スコアは情報を加えず誤解を招くのみ。
- `suggest_questions` が LLM 未接続時に `LLMError` を伝播させて 502 を返していた問題を修正。他の LLM 連携パス (Q&A 等) と同様に graceful degradation し、`[]` を返すようにした。推奨質問はベストエフォートの補助機能であり、LLM 障害がエラー応答になるのは設計原則と矛盾していた。

## [v0.1.4] - 2026-06-13
### Changed
- 引用の根拠検証を**精度優先**へ再設計 (ソクラテス問答 第4ラウンド)。v0.1.3 の「根拠が弱い」フラグは、同義語による正しい言い換え(字句重なり0)を誤帰属(同じく0)と区別できず、**正しい回答を誤って咎める偽陽性**を生んでいた。字句信号は「高重なり=根拠ありの強い正の証拠」だが「低重なり=判定不能」と非対称であることを踏まえ、確信できる主張のみ提示する設計に変更:
  - `confirmed`: 引用文が引用先ソースに字句的に裏付けられた引用 (正の信号、偽陽性なし)
  - `misattributed`: 引用文が**別の**ソースに著しく強く一致する場合のみフラグ = 番号の取り違え (高精度の負の信号、同義語言い換えとは区別可能)
  - 判定不能(言い換えの可能性)な引用は沈黙し、断罪しない
- `citation_report` の `weak` を `confirmed` + `misattributed` に置換。UIは確認済みを seiji 実塗り、取り違えを琥珀破線で表示
- README の引用検証の記述を、字句信号の限界を明記した実態に合わせて更新

## [v0.1.3] - 2026-06-13
### Added
- 引用の根拠検証 (誤帰属検出): 引用付きの各文を、その文が引くソース本文と字句(文字bigram)で照合し、ほぼ無関係なら「根拠が弱い引用」として旗を立てる。従来の範囲チェック(存在しない番号の検出)では捕まえられなかった「実在ソースへの誤帰属」を依存ゼロ・LLM不要で近似検出する。`citation_report` に `weak`(弱い根拠のS番号)と `grounding`(良好な引用文の割合)を追加
- UIで弱い根拠の蔵書印を破線・琥珀色で表示し、回答・Studio出力にバッジを付与

### Notes
- 字句シグナルゆえ助言的: 言い換え由来の偽陽性/陰性を避けるため閾値は CJK 向けに較正 (GROUNDING_MIN=0.30)。共通の係助詞・コピュラbigram(「ある」等)単独では根拠ありと誤判定しないことを回帰テストで担保

## [v0.1.2] - 2026-06-13
### Added
- ノートブック改名 API (`PATCH /api/notebooks/{id}`) + サイドバーの✎ボタン(ホバー表示)
- チャット履歴クリア API (`DELETE /api/notebooks/{id}/messages`) + 文机ペインのクリアボタン
- 追問クエリ拡張: 30文字未満の短い質問は直前のユーザー発言を先頭に連結して検索。「それを詳しく」型の追問でヒット率が向上
- `citation_report` に `source_id_map` フィールドを追加。同名ソースが複数存在しても引用ジャンプが正しいソースを開くように (後方互換: フィールド不在の旧レポートはタイトル照合にフォールバック)

### Changed
- `GroundedContext` に `source_ids` フィールドを追加し、関連コード全体で source_ids を make_report に渡すよう統一

## [v0.1.1] - 2026-06-11
### Added
- マルチターン会話: 直近の対話履歴(最大6メッセージ・各160トークン)をプロンプトに同梱し、追問(「それを詳しく」等)が成立するように。履歴中の [S番号] は文脈ごとに番号が変わるため除去して混入を防止
- 推奨質問のサーバ側キャッシュ: ソース構成が変わらない限りLLMを再呼び出ししない(ノートブックを開くたびに生成が走っていた問題を解消)

### Fixed
- 存在しないソースIDへの `/api/sources/{id}/text` が空配列ではなく404を返すように
- JSONボディがサイズ上限超過のとき、ボディを読み捨ててからエラー応答(クライアントに届く前の接続リセットを防止)
- `rename_notebook` の空文字名を拒否(作成時と同じ検証)

### Security
- ローカルWeb UIへのDNSリバインディング/CSRF防御: Hostヘッダを 127.0.0.1 / localhost / ::1 に限定し、状態変更リクエストのクロスサイトOriginを403で拒否
- clickjacking対策: UI応答に `frame-ancestors 'none'` (CSP) と `X-Frame-Options: DENY` を付与
- SQLite `busy_timeout=5000` を設定し、スレッド並行時の即時SQLITE_BUSY失敗を解消

## [v0.1.0] - 2026-06-11
### Added
- Notebook管理 (作成/一覧/削除) とソース取込 (TXT/MD/HTML/PDF/URL)
- ソース限定Q&A: BM25(FTS5 trigram) + ベクトル検索のCC融合、MMR、適応α
- 引用検証: 回答中の [S番号] を機械検証し、捏造引用をフラグ (citation hallucination対策)
- Studio出力5種: briefing / study_guide / faq / timeline / mindmap (全出力にcitation_report付与)
- 推奨質問の自動提案、ノート、エクスポート (Markdown / BibTeX / RIS)
- Web UI (単一HTML・外部リソースゼロ・ja/en・WCAG AA配慮) + SSEストリーミング回答
- CLI: notebook / add / ask / studio / questions / export / serve
- LLM未接続時のgraceful degradation (検索結果のみ提示)
- 10MBアップロード上限、127.0.0.1限定バインド、間接プロンプトインジェクション防御

### Security
- URL取込のSSRF防御をIPピン留め方式に変更し、DNSリバインディング(TOCTOU)を遮断 (ADR-001)
- 引用検証を結合形式 [S1, S2]・全角・S空白に対応させ、軽量LLM出力での捏造引用見逃しを解消
