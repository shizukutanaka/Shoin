# Plan: Shoin (書院) — ローカルNotebookLM代替

> **プロダクト定義**: このプロダクトは、プライバシーを重視する個人(研究者・学生・開発者)が手元の文書群に対して根拠(引用)付きの質問・要約・学習資料生成を行うためのセルフホスト型ノートブックアプリである。文書・質問・生成物を外部サービスへ一切送信せず、8GB RAM級の一般PCで動く軽量LLM(≤8B)のみで実用になることを設計上の制約とする。

## 名称 (確定)

**Shoin (書院)** — 知の書斎。2026-06-10 取締役決定。PyPI空き・GitHub有名衝突なし確認済み。
棄却候補: Tsuzuri(綴) / Shirabe(調) / Bunko(文庫) / Suzuri(硯, GMO SUZURI衝突) / Shiori(栞, go-shiori衝突)

## 目的

NotebookLMの中核体験(ソース限定・引用付きQ&A + Studio出力)を、外部送信ゼロ・軽量LLM・最小依存で再現したOSSをGitHub公開する。差別化軸は「引用の機械検証」「単一プロセス(Docker不要)」「日本語一次」。競合(Open Notebook / SurfSense / InsightsLM)は全て重量スタック、引用検証なし。

## 規模判定・体制

- 規模: **L** (新モジュール群、2〜3日) → Plan.md必須、/effort high
- 設計: 本セッション完了(spec.md確定済) / **実装: Sonnet** / CI・整形: Haiku
- CrossReview + /loop 適用。アーキ・課金・認証の変更なし → 付議不要

## 移植元 (車輪の再発明禁止)

**Hako v0.10.2** (`hako.zip`, 245テスト) が直接の祖先。Hako v0.8.0で「NotebookLMのgeneric・ローカル版」をnotebook層として実装済み。以下を移植・独立製品化する:

| Hako資産 | 移植内容 |
|----------|---------|
| `notebook.py` | Notebook CRUD / Studio出力5種 / ソース限定Q&A / 引用検証(`extract_citations`/`validate_citations`) / 公平トークン予算配分 |
| `rerank.py` | 依存ゼロ レキシカルリランカ + MMR |
| `index.py` | FTS5 + マイグレーション機構 / RRF k=10 / CC融合 / adaptive alpha (arXiv:2604.01733, 2604.16394) |
| `llm.py` | バックエンド自動選択 + graceful degradation パターン |
| 知見 | citation faithfulness研究 (arXiv:2412.18004, 2601.05866, 2505.04847) / バージョン3点同期 / 再帰コピー罠回避 |

Shoin = Hakoの「ファイル管理」を捨て、「Notebook UX + Web UI」に全振りした単機能製品。Hako本体は据え置き(C3: スコープ分離)。

## スコープ

**含む (v0.1.0):** Notebook CRUD / 取込(PDF・MD・TXT・HTML・URL) / チャンク+FTS5 / ハイブリッド検索(BM25+ベクトル委譲、CC融合) / リランク+MMR / 引用付きQ&A(SSE) / 引用検証+citation_report / Studio出力5種 / 推奨質問 / 手動ノート / MD・BibTeX export / 単一HTML 3ペインUI(#00C4CC) / CLI / i18n(ja/en)

**含まない:** クラウド同期 / マルチユーザー / 音声概要(→v0.2: VOICEVOX・Piper) / YouTube字幕(→v0.2) / クラウドコネクタ / 独自推論エンジン

## 技術選定 (C6: 3案比較)

| 案 | 構成 | コスト | 保守性 | 性能 | 判定 |
|----|------|--------|--------|------|------|
| **A** ◎採用 | Python 3.11+ stdlib HTTP + sqlite3(FTS5) + 単一HTML(vanilla JS)。LLM/埋め込みはOpenAI互換ローカルAPIへ委譲 | 低(Hako資産流用) | 高(依存: pypdfのみ) | 十分(I/OはLLM律速) | 採用 |
| B | Rust axum 単一バイナリ | 高(資産流用不可) | 高 | 高 | 配布性は勝るがROI負け。v2候補 |
| C | 単一HTML + WebGPU(wllama) | 中 | 低 | 低(モデルサイズ・PDF抽出・永続化制約) | 棄却 |

配布: pip / pipx / uvx。フロントはビルド工程なし(npm不使用)。

## 完成形ファイル一覧 (C11: 本Planと同時作成済み)

- `README.md` — 利用者目線(作成済)
- `docs/spec.md` — 要件P0/P1/P2・データモデル・検索パイプライン・引用検証・STRIDE(作成済)
- `docs/faq.md` — 12問(作成済)
- 実装時に追加: `LICENSE`(MIT) / `CHANGELOG.md` / `SECURITY.md` / `CONTRIBUTING.md` / `docs/adr/ADR-001-architecture.md`

## フェーズ

- [x] **Phase 1: コアエンジン** — store.py(SQLite+migration) / ingest.py(PDF・MD・TXT・HTML・URL+SSRF防御) / chunk.py / search.py(BM25+CC融合+rerank+MMR)
      DoD: 単体テスト全通過、埋め込み未設定でBM25動作、ruff+mypy --strict 0
- [x] **Phase 2: LLM層** — llm.py(OpenAI互換クライアント、SSE、graceful degradation) / qa.py(grounded QA、ソース番号付きプロンプト、インジェクション防御) / citation.py(検証+report)
      DoD: 不正引用検出率100%(合成テスト)、LLM不在でも検索のみ動作
- [x] **Phase 3: Studio + 付加機能** — studio.py(5種) / 推奨質問 / notes / export(MD・BibTeX・RIS) / CLI
      DoD: 全Studio出力にcitation_report付与、CLIで全機能到達
- [x] **Phase 4: Web UI** — index.html単一ファイル、3ペイン、引用クリック→原文ハイライト、invalid引用赤表示、SSE表示、ja/en、WCAG AA、Empty State 4種
      DoD: 主要フローE2E(取込→質問→引用ジャンプ→Studio)手動全通過
- [x] **Phase 5: SHIP** — テストカバレッジ≥50% / CI(lint→test→coverage→audit→SBOM) / gitleaks / 外部公開ドキュメント4点 / `shoin-v0.1.0.zip`
      DoD: 出荷チェックリスト全PASS、`<promise>COMPLETE</promise>`

## DoD (全体)

- [x] テスト全通過 73件(カバレッジ85%、Phase別DoD含む)
- [x] ruff + mypy --strict 警告ゼロ
- [ ] 引用検証: 範囲外引用の検出率100%
- [ ] Ollama + Qwen3-4B 実機相当で 取込→引用付き回答 が成立
- [ ] 埋め込み・LLM不在時のgraceful degradation動作
- [ ] PIIスキャン(ログ・エラー・テストデータにユーザー文書本文なし)
- [ ] GitHub公開zip(README/LICENSE/CHANGELOG/SECURITY/CONTRIBUTING/CI同梱)

## リスク

| リスク | 対策 |
|--------|------|
| 軽量LLMの引用品質不足 | 引用検証で機械検出 + 「ソースに記載なし」回答を正答扱い + プロンプトはHako実証済みテンプレ移植 |
| 間接プロンプトインジェクション(ソース内指示) | ソースのデータ区画化 + 指示無視のシステムプロンプト + 出力検証(Kaname知見) |
| SSRF(URL取込) | プライベートIP帯拒否・スキーム制限・リダイレクト上限 |
| 競合の機能量 | 追わない。軽量・検証・日本語の3点に集中(C3) |
| 埋め込み未設定環境 | BM25のみフォールバックを一級動作として常時テスト |


## 監査追補 (2026-06-11 CrossReview Critical pass)
- [x] SSRF: DNSリバインディング穴を発見→IPピン留めで修正 (ADR-001)
- [x] 引用検証: 結合形式 [S1,S2]・全角の見逃しを発見→修正
- [x] クリーンインストール検証 (隔離venv・entry point・static同梱)
- [x] SBOM生成 (CycloneDX 1.6, pypdf 5.9.0)
- [x] detect-secrets スキャン 0件 / 受取アドレス混入なし
- [x] テスト79件・カバレッジ86% / ruff・mypy --strict 警告ゼロ
