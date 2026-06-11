# Shoin (書院)

ローカルに構える自分だけの書院。文書を収め、引用付きで対話するNotebookLM代替。外部にデータを一切送らない。

## Features

- **Notebook**: PDF / Markdown / TXT / HTML / URL をソースとして束ね、その範囲だけを根拠に回答
- **引用付き回答 + 引用検証**: 回答中の `[S1][S2]` をクリックで原文へジャンプ。存在しないソースを引用したら自動検出してフラグ(citation hallucination対策)
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
```

Web UIは3ペイン構成: 左=ソース / 中央=チャット / 右=Studio・ノート。

## Configuration

環境変数または `~/.config/shoin/config.json`:

| 変数 | 既定値 | 説明 |
|------|--------|------|
| `SHOIN_LLM_URL` | `http://localhost:11434/v1` | OpenAI互換エンドポイント |
| `SHOIN_LLM_MODEL` | `qwen3:4b` | 生成モデル |
| `SHOIN_EMBED_MODEL` | `nomic-embed-text` | 埋め込みモデル(空でBM25のみ) |
| `SHOIN_DATA_DIR` | `~/.local/share/shoin` | SQLiteデータ保存先 |
| `SHOIN_PORT` | `7440` | リッスンポート(127.0.0.1固定) |
| `SHOIN_LANG` | `ja` | UI言語(ja/en) |

## Supported Models

OpenAI互換APIを話せるローカルモデルなら何でも可。動作確認済みの推奨:

| 用途 | モデル | RAM目安 |
|------|--------|---------|
| 生成(推奨) | Qwen3 4B / 8B | 4–6GB |
| 生成(軽量) | Gemma 3 4B / Phi-4-mini 3.8B | 3–4GB |
| 埋め込み | nomic-embed-text | 0.3GB |
| 埋め込み(日本語特化) | ruri-v3 | 0.5GB |

## License

MIT
