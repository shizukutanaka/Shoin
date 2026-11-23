# Shoin (書院)

**Language**: 日本語 | [English](README_EN.md)

ローカルに構える自分だけの書院。文書を収め、引用付きで対話するNotebookLM代替。外部にデータを一切送らない。

## Features

- **Notebook**: PDF / Markdown / TXT / HTML / WebVTT / URL をソースとして束ね、その範囲だけを根拠に回答
- **引用付き回答 + 機械的な引用検証**: 回答中の `[S1][S2]` をクリックで原文へジャンプ——引用に使われたチャンク自体がハイライトされ、節名・検出経路(全文/意味)まで表示。(1) 存在しないソース番号の検出、(2) 字句的裏付けの「根拠確認済み」表示、(3) **別の**ソースとの強一致による番号取り違え検出(正しい出典を提示)、(4) 無出典断定文の検出(出典内一致は引用欠落として区別し最尤出典を提示)に加え、数値(倍率/漢数字/英数詞/歩合/率表記/元号橋渡し)・単位・否定反転・逐語引用の改竄・回答内自己矛盾・繰返し退化まで機械検査。字句信号の限界(同義語言い換えは判定不能)を踏まえ、確信できる場合のみ提示し正しい回答を誤って咎めない。LLM不要・依存ゼロ
- **Studio出力**: ブリーフィング / 学習ガイド / FAQ / 年表 / マインドマップをソースから生成。各出力は1クリックでノートに保存できる
- **ノート**: ノートブックに自由メモを保存。Studio出力のノート化にも対応
- **エクスポート**: ノートブック全体をMarkdown(ノート・Studio出力・対話履歴込み)、引用文献をBibTeX/RISで出力
- **完全ローカル**: 文書・質問・生成物は外部サービスへ送信されない。オフライン動作
- **軽量**: 8GB RAM級のPCで動く軽量LLM(Qwen3-4B等、≤8B)を前提に設計。Docker不要・単一プロセス

## Installation

```bash
# ソースから (現時点で唯一の方法。動作検証済み)
git clone https://github.com/shizukutanaka/Shoin.git && cd Shoin
pip install .

shoin serve              # 起動したら http://localhost:7440 をブラウザで開く
```

`pip install` せずにソースツリーから直接動かす場合は `python -m shoin serve` も使えます(v0.2.459 以降、両者は同一エントリに委譲)。

```bash
# 単一ファイル .pyz (インストール不要・Python 3.11+ のみ必要)
# ビルド済みは GitHub Releases (v0.2.746) に shoin.pyz として添付している
# ソースから生成する場合は:
python3 scripts/build_pyz.py        # dist/shoin.pyz を生成
python3 dist/shoin.pyz serve        # 全機能が1ファイルで動く
```

> **PyPI は未公開です。** `pip install shoin` はまだ動きません(公開には
> メンテナの認証情報が必要)。リポジトリから直接入れる場合は **ref を明示**してください —
> 既定ブランチは古い版を指していることがあります:
>
> ```bash
> pip install "git+https://github.com/shizukutanaka/Shoin.git@main"
> ```

前提: [Ollama](https://ollama.com)、llama.cpp、LM Studio いずれかのOpenAI互換ローカルエンドポイント。

```bash
ollama pull qwen3:4b              # 生成用(推奨)
ollama pull nomic-embed-text      # 埋め込み用(任意。無くてもBM25検索で動作)
```

LLMの用意ができているか分からない場合は、初回セットアップウィザードが案内します:

```bash
shoin setup                       # LLM検出 → 構成保存 → サンプル文書で引用付き回答を体験
```

ウィザードは現在の設定・Ollama・LM Studio・llama.cpp の順にローカルエンドポイントを検出し、応答したものを `~/.config/shoin/config.json` に保存します。見つからない場合は理由と環境別の導入手順を表示して終了します（ダウンロードやインストールを自動で始めることはありません）。接続できたら、サンプル文書だけのノートブック「Shoin サンプル」を用意し、引用付き回答が返るところまでをその場で体験できます。`--url` で任意のOpenAI互換エンドポイントの指定、`--no-save` で保存の省略、`--skip-demo` で体験の省略ができます。構成ファイルが無い状態で `shoin serve` を起動した場合も、開始前にウィザードへの案内が1行表示されます。

## Usage

```bash
shoin setup                           # 初回セットアップ (LLM検出→構成保存→サンプル体験)
shoin serve                           # Web UI起動
shoin notebook new "研究ノート"        # CLIでも操作可
shoin notebook duplicate 1             # ノートブックを複製(ソース/チャンク/ノート全コピー)
shoin notebook merge 1 2               # nb2をnb1へ統合(元nbはゴミ箱へアーカイブ)
shoin notebook settings 1 top_k=4 source_text_tokens=512  # nb単位の検索上書き(未設定=グローバル既定)
shoin add 1 ./paper.pdf https://example.com/article
shoin add 1 ~/papers/                 # フォルダ一括取込(再実行で新規追加+変更分のみ更新)
shoin source missing 1 ~/papers/      # 元ファイルが見つからないソースのIDとパスを確認
shoin ask 1 "この論文の主要な貢献は?"
shoin studio 1 study_guide
shoin health                          # 設定・LLM到達性を確認(headless診断)
shoin eval 1 cases.json               # 検索精度を自分の文書で測定(recall/MRR)
# その他: search / questions / messages / note / source (rename・refresh・refresh-all・weight・meta・delete) / chunk edit / reindex
#         stats(利用メトリクス表示。GET /api/metricsでも同一カウンタを返す) / trash(list|restore|purge|empty 削除済みnbのundo) / backup
#         vacuum(FTS索引を明示マージし削除済み領域をOSへ返却——DB物理縮小) / check(DB診断——物理整合性+埋込み欠落、破損時rc=1)
#         export --format tree → shoin import で別マシン/別DBへnbを受け渡し (shoin-nb-tree-v1)
```

Web UIは3ペイン構成: 左=ソース / 中央=チャット / 右=Studio・ノート。
Studio のマインドマップは、階層箇条書きとして解析できるときに図で表示します。クリックできる引用付きの本文も図の下に残ります。

`source missing` は指定フォルダ内の元ファイルを確認するだけで、索引は変更しません。
外部ドライブなどが接続されていることを確認したうえで、不要なソースだけを
`shoin source delete <source_id>` でゴミ箱へ移動できます。復元は `shoin trash list` で
ゴミ箱内のIDを調べ、`shoin trash restore <trash_id>` を実行します。

Obsidian の vault など、ローカルフォルダの追加・更新を自動で取り込むには
`shoin watch 1 ~/my-vault` を実行します。約2秒ごとに確認し、Ctrl-C で停止します。
`--once` を付けると1回だけ確認します。通常の取込と同じ対応形式・上限で、
`.obsidian` や `.trash` などの隠し項目とシンボリックリンクは対象外です。
元ファイルの削除・改名では索引を削除しません。元のパスは `source missing` で確認できます。
改名先は新しいパスとして扱いますが、既存ソースと同一内容なら通常の取込と同様に重複登録しません。

### YouTube の字幕を取り込む

YouTube Studio などから書き出した WebVTT (`.vtt`) ファイルを、Web UI の
ファイル選択または `shoin add 1 ./captions.vtt` で取り込めます。
タイムコードや表示用タグを除いた字幕本文が検索・引用の対象になります。
動画 URL から字幕を自動取得する機能ではありません。

### 音声概要 (CLI)

[Piper](https://github.com/OHF-Voice/piper1-gpl) をインストールして `piper` を PATH に通し、
ブリーフィングの言語に合う音声モデル (`.onnx` と同名の `.onnx.json`) を手元に用意してください。

```bash
shoin studio 1 briefing
shoin audio 1 --model ./voice.onnx --output ./briefing.wav
```

[VOICEVOX](https://voicevox.hiroshiba.jp/) で日本語を読み上げるには、
エンジンを `127.0.0.1:50021` で起動し、`/speakers` で確認したスタイル ID を指定してください
（例では 3）。

```bash
shoin audio 1 --voicevox-speaker 3 --output ./briefing-ja.wav
```

VOICEVOX エンジン自身のローカル HTTP アクセスログには、読み上げる本文が残ることがあります。

`audio` は最新の保存済みブリーフィングを使うため、LLM に再接続せずオフラインで動作します。
音声では引用番号を読み上げません。引用は元の Studio 出力で確認できます。
モデルの自動ダウンロードや既存 WAV ファイルの上書きはしません。

Web UI で音声を取得するには、`shoin serve --piper-model ./voice.onnx` または
`shoin serve --voicevox-speaker 3` で起動し、Studio の保存済みブリーフィングで
**音声をダウンロード (WAV)** を選んでください。音声設定なしで起動した場合、ボタンは表示されません。

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
| `SHOIN_LLM_URL` | `http://localhost:11434/v1` | OpenAI互換エンドポイント。非ローカル指定時は起動時/`shoin health`/`/api/health`(`llm_external`)に警告——文書は外部送信される。URL内資格情報(user:pass@)は表示時に除去され秘密は出ない |
| `SHOIN_LLM_MODEL` | `qwen3:4b` | 生成モデル |
| `SHOIN_LLM_API_KEY` | (無効) | 設定すると全LLMリクエストへ`Authorization: Bearer <key>`を付与。認証必須のゲートウェイ(vLLM+proxy・ホスト型OpenAI互換)向け。未設定時はヘッダ自体を送らない。`http://`外部エンドポイントと併用時は起動時に平文転送の警告が出る(https://推奨) |
| `SHOIN_EMBED_MODEL` | `nomic-embed-text` | 埋め込みモデル(空でBM25のみ) |
| `SHOIN_DATA_DIR` | `~/.local/share/shoin` | SQLiteデータ保存先 |
| `SHOIN_PORT` | `7440` | リッスンポート(127.0.0.1固定) |
| `SHOIN_LANG` | `ja` | UI言語(ja/en)。CLI・エクスポート・Web UI 全てに適用。Web UI は初回表示時の既定値としてのみ使う — ヘッダーの言語切替ボタンを押すとブラウザに記憶され、以後はその選択が優先される(v0.2.177) |
| `SHOIN_MULTI_QUERY` | (無効) | `1`でマルチクエリRAG-Fusion検索を有効化。質問をLLMで複数の言い換えに展開し検索結果をRRF統合(再現率向上。ask毎にLLM呼び出しが1回増える) |
| `SHOIN_EMBED_BATCH` | `16` | 埋め込みリクエストのバッチサイズ(エンドポイント能力に合わせて調整) |
| `SHOIN_LLM_RETRIES` | `2` | chat/embed呼出しの追加リトライ回数(0-5)。接続拒否・タイムアウト等の一時的輸送失敗のみ指数バックオフで再試行。HTTPエラー・応答異常は再試行しない。接続失敗で再試行を使い切った後は復帰するまで以降1回だけ試行し、成功すると通常の予算に戻る。0で無効、不正値は既定に戻る |
| `SHOIN_CHUNK_TOKENS` | `512` | チャンク分割の目安トークン数。`shoin eval` の前後で変えて自分の文書での効果を測定できる(次回の取込/再インデックスから有効) |
| `SHOIN_CHUNK_OVERLAP` | `64` | チャンク間のオーバーラップトークン数(チャンクサイズ未満、負値・超過は既定に戻る)。オーバーラップの効果は文書依存で一律ではないため測定推奨 |
| `SHOIN_DEBUG` | (無効) | `1`で検索の診断情報(BM25/vectorヒット数、RRF順位、最終スコア)を標準エラー出力に表示 |
| `SHOIN_LOG_JSON` | (無効) | `1`で取込・回答イベントをJSON Linesで標準エラー出力へemit(`{"ts","event",…}`、`source_indexed`/`ask_completed`——ID・件数・msのみ、本文非含有) |
| `SHOIN_EXPORT_CREDIT` | `1`(有効) | `0`でMarkdown/BibTeXエクスポート末尾の帰属表示1行(「Shoinでエクスポート — リポジトリURL」)を付けない。行自体はプレーンテキストなので出力から削除してもよい。RIS・JSONツリー等の機械可読形式には付かない |
| `SHOIN_THEME_CSS` | `~/.config/shoin/theme.css` | ユーザーテーマCSSのパス。配色は全てCSS変数のため `:root{--washi:…}` を上書きするだけで見た目を変えられる。Web UIが`/api/theme.css`経由で読込む(未存在・256KiB超は空スタイルシートに降格) |
| `SHOIN_ACCEL` | `1`(有効) | 任意のRust高速化部品(`rust/shoin-accel/`でビルドした`shoin/_shoin_accel.so`)を使うかどうか。部品が無い環境では常に従来の純Python経路で動作し、結果は同一。`0`で部品があっても無効化できる |

`shoin eval` の cases.json 例 — 設定変更(例: `SHOIN_MULTI_QUERY=1`)の前後で実行すれば、
文献の数値ではなく**自分の文書での効果**を比較できる:

```json
[
  {"q": "和紙はどう作られるか", "sources": [1]},
  {"q": "活版印刷の仕組みは", "sources": [2, 3]}
]
```

ベースライン比較: `--save` で基準実行を保存し、`--diff` で差分を表示する。
差分の集計は両実行に共通する質問のみで計算し、追加・削除された質問は別枠で一覧する
(質問文の一致で対応付けるため、casesファイルの行順変更や編集で偽の回帰は出ない)。
同一質問の重複は出現順にペアリングされる。

`--gen` でノートブックからケース雛形を生成できる(チャンクを持つソース1件=1ケース、
`sources` は実際のid——手直し前提の雛形):

```bash
shoin eval 1 --gen > cases.json                # 雛形を生成してから手直し
shoin eval 1 cases.json --save before.json   # 設定変更前
shoin eval 1 cases.json --diff before.json   # 設定変更後: recall/MRR の ± を表示
```

`config.json` の例:

```json
{
  "SHOIN_LLM_MODEL": "qwen3:8b",
  "SHOIN_EMBED_MODEL": "ruri-v3"
}
```

## Performance

実測値(768次元の埋め込み、開発機。LLMの生成時間は別途かかり、通常はこれより大きい):

| ノートブック規模 | 検索1回 | 備考 |
|---|---|---|
| 1,000 チャンク | 約20 ms | |
| 5,000 チャンク | 約120 ms | |
| 20,000 チャンク | 約380 ms | |
| 50,000 チャンク(上限) | 約0.94 s | `MAX_CHUNKS_PER_NOTEBOOK` |

| 取り込み(1ソース) | 所要 |
|---|---|
| 1 MB の Markdown | 約0.3 s |
| 5 MB | 約1.5 s |
| 9.4 MB(上限10MB直下) | 約3.0 s |

- 検索時間はチャンク数にほぼ比例します。**対話的な応答(100ms未満)を保ちたい場合はノートブックを5,000チャンク程度までに分けてください** — 大きな1冊より、主題ごとに分けた複数冊のほうが速く、引用も追いやすくなります。
- 引用検証は13万文字・8ソースでも15ms未満、ノートブックの読み込みは300ソース・200メッセージで約9ms。どちらも体感には影響しません。
- メモリはクエリのチャンク数に依存しません(上位k件のみ実体化)。

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
pip install -e . && pip install -r requirements-dev.txt    # 依存(固定版)
./scripts/verify.sh                                    # 全ゲート(lint/型/テスト+カバレッジ/secret scan)
git config core.hooksPath .githooks                    # push前に自動実行(1回だけ)
```

`scripts/verify.sh` は `ci/ci.yml` と同じ検証を1コマンドで実行する。
`.githooks/pre-push` を有効化すると push 前に自動で走り、失敗すれば push を止める
(緊急時は `git push --no-verify`)。GitHub Actions の権限が無い環境でも
「landする前に自動検証される」という要件をこれで満たす — 詳細は `ci/README.md`。

### バージョンとリリース

- バージョンの正本 (source of truth) は `pyproject.toml` の `project.version` で、
  現在のバージョンは **v0.2.746** です。`shoin.config.VERSION` と `CLAUDE.md` の
  `Current version` も同じ値を指します (テスト `test_version_markers_agree` が固定)。
- リリースタグは `v<pyproject.toml のバージョン>` (例: `v0.2.746`) とします。
- タグはリリース時に切ります。現在のバージョン (v0.2.746) にはタグを切り、
  GitHub Releases にビルド済みの shoin.pyz を公開しています。過去のタグ
  (`v0.2.474` 等) は現在のバージョンを示しません — 最新版は Release の
  shoin.pyz か、ソースから入れてください (Installation 参照)。

## Support / 商用サポート

Shoin is MIT-licensed and free forever, with no feature gating.
Do not paste document contents or confidential company information into public issues.

## License

MIT
