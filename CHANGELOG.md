# Changelog

## [Unreleased]

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
