# Changelog

## [Unreleased]

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
