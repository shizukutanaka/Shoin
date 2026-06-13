# Changelog

## [Unreleased]

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
