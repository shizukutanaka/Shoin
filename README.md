# Shoin (書院)

ローカルに構える自分だけの書院。文書を収め、引用付きで対話するNotebookLM代替。外部にデータを一切送らない。

## Features

- **Notebook**: PDF / Markdown / TXT / HTML / URL をソースとして束ね、その範囲だけを根拠に回答
- **引用付き回答 + 四段の引用検証**: 回答中の `[S1][S2]` をクリックで原文へジャンプ。(1) 存在しないソース番号を検出、(2) 引用文がソース本文に字句的に裏付けられていれば「根拠確認済み」と表示、(3) 引用文が**別の**ソースに強く一致する場合は番号の取り違えとして検出、(4) 引用が一切ない断定文(無出典の主張)を検出。字句信号の限界(同義語言い換えは判定不能)を踏まえ、確信できる場合のみ提示し正しい回答を誤って咎めない。LLM不要・依存ゼロ
- **Studio出力**: ブリーフィング / 学習ガイド / FAQ / 年表 / マインドマップをソースから生成
- **完全ローカル**: 文書・質問・生成物は外部サービスへ送信されない。オフライン動作
- **軽量**: 8GB RAM級のPCで動く軽量LLM(Qwen3-4B等、≤8B)を前提に設計。Docker不要・単一プロセス

## Installation

```bash
pip install shoin        # または: pipx install shoin / uvx shoin
shoin serve              # http://localhost:7440 が開く
```

前提: [Ollama](https://ollama.com)、llama.cpp、LM Studio いずれかのOpenAI互換ローカルエンドポイント。

```bash
ollama pull qwen3:4b              # 生成用(推奨)
ollama pull nomic-embed-text      # 埋め込み用(任意。無くてもBM25検索で動作)
```

## Usage

```bash
shoin serve                           # Web UI起動
shoin notebook new "研究ノート"        # CLIでも操作可
shoin add 1 ./paper.pdf https://example.com/article
shoin ask 1 "この論文の主要な貢献は?"
shoin studio 1 study_guide
shoin health                          # 設定・LLM到達性を確認(headless診断)
shoin eval 1 cases.json               # 検索精度を自分の文書で測定(recall/MRR)
```

Web UIは3ペイン構成: 左=ソース / 中央=チャット / 右=Studio・ノート。

### 検索の挙動(日本語)

BM25(FTS5トライグラム)+ ベクトルのハイブリッド検索。日本語の表記ゆれを検索時に自動で吸収する:

- **幅・字体の相互一致**: 全角カナ(データベース)/ 半角カナ(ﾃﾞｰﾀﾍﾞｰｽ、cp932由来の旧データに多い)/ 全角英数(ＧＰＵ、２０２４)/ ASCII(GPU) は互いに一致する。索引本文は原文のまま保持し、クエリ側でバリアントに展開する(v0.2.144)。
- **カタカナ↔ひらがな**: コード / こーど のような同語異script を橋渡し(v0.2.42)。
- **除外検索**: 語の前に `-` を付けるとその語を含むチャンクを除外(例: `Python -legacy`、`検索 -旧版`)。本文・節見出しの両方に作用する。
- 埋め込みモデル未設定時はBM25のみで動作(第一級モード、フォールバックではない)。

## Configuration

環境変数または `~/.config/shoin/config.json`(環境変数が優先):

| 変数 | 既定値 | 説明 |
|------|--------|------|
| `SHOIN_LLM_URL` | `http://localhost:11434/v1` | OpenAI互換エンドポイント |
| `SHOIN_LLM_MODEL` | `qwen3:4b` | 生成モデル |
| `SHOIN_EMBED_MODEL` | `nomic-embed-text` | 埋め込みモデル(空でBM25のみ) |
| `SHOIN_DATA_DIR` | `~/.local/share/shoin` | SQLiteデータ保存先 |
| `SHOIN_PORT` | `7440` | リッスンポート(127.0.0.1固定) |
| `SHOIN_LANG` | `ja` | UI言語(ja/en) |
| `SHOIN_MULTI_QUERY` | (無効) | `1`でマルチクエリRAG-Fusion検索を有効化。質問をLLMで複数の言い換えに展開し検索結果をRRF統合(再現率向上。ask毎にLLM呼び出しが1回増える) |
| `SHOIN_EMBED_BATCH` | `16` | 埋め込みリクエストのバッチサイズ(エンドポイント能力に合わせて調整) |
| `SHOIN_CHUNK_TOKENS` | `512` | チャンク分割の目安トークン数。`shoin eval` の前後で変えて自分の文書での効果を測定できる(次回の取込/再インデックスから有効) |
| `SHOIN_CHUNK_OVERLAP` | `64` | チャンク間のオーバーラップトークン数(チャンクサイズ未満、負値・超過は既定に戻る)。オーバーラップの効果は文書依存で一律ではないため測定推奨 |
| `SHOIN_DEBUG` | (無効) | `1`で検索の診断情報(BM25/vectorヒット数、RRF順位、最終スコア)を標準エラー出力に表示 |

`shoin eval` の cases.json 例 — 設定変更(例: `SHOIN_MULTI_QUERY=1`)の前後で実行すれば、
文献の数値ではなく**自分の文書での効果**を比較できる:

```json
[
  {"q": "和紙はどう作られるか", "sources": [1]},
  {"q": "活版印刷の仕組みは", "sources": [2, 3]}
]
```

`config.json` の例:

```json
{
  "SHOIN_LLM_MODEL": "qwen3:8b",
  "SHOIN_EMBED_MODEL": "ruri-v3"
}
```

## Supported Models

OpenAI互換APIを話せるローカルモデルなら何でも可。動作確認済みの推奨:

| 用途 | モデル | RAM目安 |
|------|--------|---------|
| 生成(推奨) | Qwen3 4B / 8B | 4–6GB |
| 生成(軽量) | Gemma 3 4B / Phi-4-mini 3.8B | 3–4GB |
| 埋め込み | nomic-embed-text | 0.3GB |
| 埋め込み(日本語特化) | ruri-v3 | 0.5GB |

## Development

```bash
pip install -e . && pip install ruff mypy coverage    # 依存
./scripts/verify.sh                                    # 全ゲート(lint/型/テスト+カバレッジ)
git config core.hooksPath .githooks                    # push前に自動実行(1回だけ)
```

`scripts/verify.sh` は `ci/ci.yml` と同じ検証を1コマンドで実行する。
`.githooks/pre-push` を有効化すると push 前に自動で走り、失敗すれば push を止める
(緊急時は `git push --no-verify`)。GitHub Actions の権限が無い環境でも
「landする前に自動検証される」という要件をこれで満たす — 詳細は `ci/README.md`。

## License

MIT
