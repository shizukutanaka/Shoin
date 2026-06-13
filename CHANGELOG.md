# Changelog

## [Unreleased]

## [v0.1.41] - 2026-06-13
### Fixed
- **LLM タイムアウトと接続拒否が同一エラーコード `SYSTEM_SERVICE_UNAVAILABLE` になる**: `_post()` と `chat_stream()` が `except OSError` でネットワークエラーを一括捕捉していたため、タイムアウト (`TimeoutError` = `socket.timeout`) と接続拒否 (`ConnectionRefusedError`) を区別できなかった。urllib は `socket.timeout` を `URLError(reason=TimeoutError(...))` にラップするため、`exc.reason` が `TimeoutError` かをチェックし、タイムアウト時は `SYSTEM_LLM_TIMEOUT` を送出するよう修正。接続拒否は引き続き `SYSTEM_SERVICE_UNAVAILABLE`。

## [v0.1.40] - 2026-06-13
### Fixed
- **`add_message()` がノートブック存在確認なしに INSERT し FK 制約エラーが CLI で未捕捉例外になる**: 存在しない `notebook_id` で `shoin ask 99999 "question"` を実行すると `retrieve()` は空リストを返すが、その後 `add_message()` が `sqlite3.IntegrityError` を送出し CLI の `except (StoreError, IngestError, LLMError)` に捕捉されず Python トレースバックが表示されていた。`self.get_notebook(notebook_id)` ガードを追加し、`add_note()`・`add_studio_output()` と同じパターンで `NOTEBOOK_NOT_FOUND` を送出するよう統一。

## [v0.1.39] - 2026-06-13
### Fixed
- **`add_studio_output()` がノートブック存在確認なしに INSERT し FK 制約エラーで不正な例外が伝播する**: 存在しない `notebook_id` を渡すと `sqlite3.IntegrityError` (FOREIGN KEY constraint failed) が未捕捉例外として発生し、呼び出し元の Studio 生成が曖昧なエラーで失敗していた。`add_note()` と同じパターンで `self.get_notebook(notebook_id)` ガードを追加し、`NOTEBOOK_NOT_FOUND` (StoreError) を送出するよう統一。

## [v0.1.38] - 2026-06-13
### Security
- **HTTP API の `/sources` エンドポイントがファイルパスを `target` として受け入れ、サーバー上の任意ファイルを読み込む「混乱した代理人」攻撃が可能だった**: `_h_src_add()` が `target` をそのまま `index_source()` に渡していたため、`{"target": "/etc/passwd"}` のようなリクエストでサーバープロセスが読めるファイルをノートブックへ取り込めた。ファイルパスによる投入は CLI 専用機能であるため、HTTP API では `http://` または `https://` で始まらない `target` を `INGEST_UNSUPPORTED_FORMAT` (400) で拒否するよう修正。

## [v0.1.37] - 2026-06-13
### Fixed
- **`embed()` がサーバーから要求数より少ないベクターを返された場合にサイレントにデータを欠損させる**: サーバーが 16 テキストのリクエストに対して 14 ベクターしか返さなかった場合、`_embed_chunks()` の `zip(batch_ids, vectors)` が末尾 2 チャンクの埋め込みを黙って破棄していた。`embed()` に `len(vecs) != len(texts)` の検証を追加し、不一致の場合は `SYSTEM_LLM_BAD_RESPONSE` を送出するよう修正。`embed_one()` で 0 件返却された場合の `IndexError` も同様に修正。

## [v0.1.36] - 2026-06-13
### Fixed
- **SSE ストリーミング中の `ConnectionResetError` (ECONNRESET) が捕捉されずアシスタントメッセージが永続化されない**: `_h_ask_sse()` が `except BrokenPipeError:` (EPIPE) のみを捕捉しており、Linux でクライアントが接続を切断した際に多発する `ConnectionResetError` (ECONNRESET) が未捕捉だった。4 箇所すべてを `except ConnectionError:` (BrokenPipeError と ConnectionResetError の共通親クラス) に統一し、どちらの切断方式でもアシスタントの返答が DB に保存されるよう修正。
- **`_bib_escape()` がバックスラッシュをエスケープしない**: Windows パス (`C:\Users\file.txt`) やバックスラッシュを含む LaTeX コマンドをソースタイトル/オリジンに持つ場合、エクスポートした BibTeX がパーサーにとって不正になる (`\U` や `\f` が LaTeX マクロとして解釈される)。`_bib_escape()` に `.replace("\\", "\\\\")` を先頭に追加し、他の置換と二重エスケープが起きないよう正しい順序で適用。

## [v0.1.35] - 2026-06-13
### Fixed
- **`mmr()` が同一テキストを持つ Hit の削除に `pool.remove()` を使用し誤った要素を削除する可能性があった**: `list.remove(x)` はデータクラスの等価比較で最初にマッチした要素を削除するため、chunk_id が異なるが text が同一の Hit が複数存在する場合に誤って前の要素が削除されてた。インデックスで追跡する `pool.pop(best_idx)` に変更し、常に正しい要素を 1 回の O(1) 操作で取り出すよう修正。
- **`_truncate_tokens(text, 0)` と `_truncate_tokens(text, -n)` がフルテキストを返す**: `limit <= 0` の場合、ループが開始され `acc > limit` が即座に真になるべきだが、最初のイテレーション前に評価されないためフルテキストが返っていた。`limit <= 0` の早期リターン `""` を追加。
- **`_FALLBACK_SCAN_LIMIT` が `bm25_search()` 内部で毎回再定義されていた**: 定数がローカルスコープで定義されていたため、呼び出しのたびに再割り当てが発生し、また外部からの参照(テスト・監視等)が不可能だった。モジュールレベルに移動。

## [v0.1.34] - 2026-06-13
### Fixed
- **`_embed_chunks()` と `_cmd_reindex()` の空白のみの埋め込みモデル判定が不一致**: v0.1.29 で `qa.py` と `llm.py` の判定を `.strip()` で修正したが、`pipeline.py` の `_embed_chunks()` と `cli.py` の `_cmd_reindex()` は `not llm.embedding_model` (`.strip()` なし) のままだった。ホワイトスペースのみの `SHOIN_EMBED_MODEL="  "` が truthy と判定され `LLMError` を経由してサイレントに 0 埋め込みになっていた。両箇所を `not (llm.embedding_model or "").strip()` に統一。

## [v0.1.33] - 2026-06-13
### Fixed
- **存在しないノートブックへのノート追加・ソースアップロードが FK 制約エラーでサイレントに失敗する**: `add_note()`, `_h_src_add()`, `_h_src_upload()` が存在しない `notebook_id` を受け取った場合、`sqlite3.IntegrityError` が発生してサーバーに未処理例外が伝播し、クライアントはレスポンスなしの接続切断を受け取っていた。各エントリーポイントに `store.get_notebook(nb_id)` ガードを追加し、`NOTEBOOK_NOT_FOUND` → 404 を返すよう修正。

## [v0.1.32] - 2026-06-13
### Performance
- **埋め込みバッチのコミットを 1 チャンクごとから 1 バッチごとに削減**: `_embed_chunks()` が `store.set_embedding()` を呼び出すたびに `commit()` を実行していた。バッチ 16 チャンクを処理する場合、16 回の個別コミットが発生していた。`set_embedding(commit=False)` オプションを追加し、バッチ末尾で 1 回だけ `conn.commit()` するよう変更。100 チャンクの再インデックスで SQLite トランザクション数が 100 → 7 に削減。

### Fixed
- **SSE デルタストリームで JSON パースエラーがあると接続が切断される**: バックエンドが不正な JSON フレームを送信した場合 (例: ストリーミング中断時の部分フレーム)、`JSON.parse(data)` が例外を投げてストリームリーダーが終了していた。`try-catch` でパースを保護し、不正フレームを `continue` でスキップするよう修正。

## [v0.1.31] - 2026-06-13
### Fixed
- **`set_embedding()` がコミット後にローカウントをチェックしていた**: `UPDATE` を実行してから `commit()` し、その後 `cur.rowcount == 0` をチェックしていたため、存在しないチャンクへの埋め込みが DB にコミットされてから例外を送出していた。`rowcount` チェックを `commit()` の前に移動し、無効な書き込みが永続化されないよう修正。
- **CLI (cli.py) が Ctrl+C で Python トレースバックを表示する**: `main()` が `KeyboardInterrupt` をキャッチしていなかった。`except KeyboardInterrupt: return 130` を追加し、ユーザーフレンドリーな終了コードで静かに終了するよう修正。
- **`fetch_url()` がリダイレクトループを検出しない**: `A -> B -> A` のような循環リダイレクトが最大リダイレクト数を消費するまで繰り返されていた。訪問済み URL を `set[str]` で追跡し、再訪した場合に `INGEST_URL_BLOCKED: redirect cycle detected` を即座に送出するよう修正。
- **`fetch_url()` が空のレスポンスボディで解析を試みる**: HTTP 204 相当のレスポンスで空ボディが返った場合、`html_to_text()` や `pdf_to_text()` を無駄に呼び出していた。ボディ取得直後に空チェックを追加し、`INGEST_EMPTY` を送出するよう修正。

## [v0.1.30] - 2026-06-13
### Fixed
- **`bm25_search()` フォールバック LIKE スキャンが無制限のフルテーブルスキャンを実行する**: FTS5 インデックスにヒットしなかった場合のフォールバックパスで `LIMIT` 句がなく、ノートブックのチャンク数に比例した O(n) スキャンが発生していた。`LIMIT 2000` を追加し、最悪ケースを制限。
- **`overview_hits(per_source=0)` が誤って各ソースから 1 チャンクを返す**: `per_source <= 1` の分岐が `per_source=0` も `[0]`(seq 0 のチャンク)を取得する扱いにしていた。`per_source <= 0` の場合はソースをスキップするよう修正。
- **`export_markdown()` で note/studio_output の body が NULL の場合に `"None"` が出力される**: `str(n["body"])` が `None` に対して `"None"` を返していた。`str(n["body"] or "")` に変更。

### Performance
- `bm25_search` フォールバックの最大スキャン行数を定数 `_FALLBACK_SCAN_LIMIT = 2000` で管理

## [v0.1.29] - 2026-06-13
### Fixed
- **アップロードファイル名にパストラバーサル文字が含まれてもタイトルに保存される**: `X-Filename: ../../evil/secret.txt` のようなヘッダーが URL デコード後にそのままタイトルフィールドへ保存されていた。`Path(raw_name).name` でベースネームのみを使用するよう修正。
- **空白のみの `SHOIN_EMBED_MODEL` が設定されているとベクター検索が有効になる**: `"  "` のような空白文字列は falsy ではないため `if not llm.embedding_model:` の判定をすり抜け、無効な設定でエンドポイントを呼び出していた。`.strip()` を追加して空白を除去してから判定するよう修正 (`qa.py`、`llm.py`)。
- **SSE "delta" フレームに `text` フィールドがない場合に `"undefined"` がチャットに追記される**: `j.text` が `undefined` の場合 `acc += j.text` が `"...undefined"` を生成していた。`j.text ?? ""` で null/undefined を空文字列にフォールバックするよう修正。

### Performance
- **`messages(notebook_id, id DESC)` の複合インデックス追加 (Migration 4)**: `list_messages_recent()` は `ORDER BY id DESC LIMIT ?` で最新 N 件を取得するが、既存の `idx_messages_notebook` は `notebook_id` のみのインデックスのため、全行を取得してからソートする必要があった。`idx_messages_notebook_id_desc ON messages(notebook_id, id DESC)` を追加し、O(limit) でスキャンできるよう改善。

## [v0.1.28] - 2026-06-13
### Fixed
- **`_notebook_json()` が壊れた `citation_report` JSON でクラッシュ**: `GET /api/notebooks/{id}` が studio_outputs と messages の `citation_report` フィールドを `json.loads()` するが、`try-except` がなかった。`export.py` と同様の問題。`_safe_report()` ヘルパーを追加し、両箇所で使用。
- **SSE "done" イベントで `j.report` が存在しない場合に TypeError**: `j.report.invalid.length` が `j.report` 未定義でクラッシュしていた。`const rpt = j.report || {}` でガードし、全プロパティアクセスをオプショナルチェーン(`?.`)に変更。
- **チャット SSE ストリーミング中に自動スクロールしない**: `ev==="delta"` でテキストを更新するたびに `#chat.scrollTop = #chat.scrollHeight` を呼び出すよう修正。`ev==="done"` 時にも追加。

### Changed
- **UI の `cur.sources.length` をオプショナルチェーンに変更**: `cur.sources?.length` を使用し、API レスポンスの形式変更に対して堅牢にした。

## [v0.1.27] - 2026-06-13
### Fixed
- **`LLMClient.chat()` が `content: null` レスポンスに対して文字列 `"None"` を返す**: 一部のモデル(Qwen3 思考モード・function calling など)は `choices[0].message.content` を `null` で返す場合がある。`str(None)` = `"None"` がアシスタントメッセージとして永続化・表示されていた。`content is None` の場合は `SYSTEM_LLM_BAD_RESPONSE` を送出するよう修正。呼び出し元はこのエラーをデグラデーション処理で扱う。

### Added
- **`export_markdown()` のセクションヘッダーを i18n 対応**: `"## ソース"` / `"## ノート"` / `"## Studio出力"` / `"## チャット履歴"` / `"引用元:"` を `_STRINGS`/`_t()` で管理。`SHOIN_LANG=en` で `## Sources` / `## Notes` / `## Studio Output` / `## Chat History` / `(sources:)` が使用される。

## [v0.1.26] - 2026-06-13
### Fixed
- **`generate()` が存在しない notebook に対して `NOTEBOOK_NOT_FOUND` でなく `NOTEBOOK_EMPTY` を返す**: `overview_hits()` は存在しない notebook_id に対して空リストを返すため、呼び出し元では「ソースなし」と「ノートブック不在」を区別できなかった。`store.get_notebook(notebook_id)` を `overview_hits()` の前に追加し、正しいエラーコードを送出するよう修正。
- **`suggest_questions()` が存在しない notebook に対してサイレントに `[]` を返す**: 同様の問題。`store.get_notebook()` を先頭に追加し、`NOTEBOOK_NOT_FOUND` を送出するよう修正。

### Added
- **`studio.py` の全ユーザー向けメッセージを i18n 対応**: `_INSTRUCTIONS`(5種類: briefing / study_guide / faq / timeline / mindmap)、セクションヘッダー(`ソース`/`指示`)、引用注記、質問プロンプトテンプレートを `_STRINGS`/`_INSTRUCTIONS` 辞書に集約。`SHOIN_LANG=en` で英語のLLMプロンプトが使用される。`generate()` と `suggest_questions()` は `SYSTEM_PROMPT` 定数ではなく `_qa_t("system_prompt")` を呼び出し時に評価するよう変更。

## [v0.1.25] - 2026-06-13
### Fixed
- **SSE no-hit レスポンスが `SHOIN_LANG` を無視**: `_h_ask_sse()` がクエリに一致するソースなしの場合に `NO_HIT_TEXT` 定数(常に日本語)を使用していた。v0.1.24 の `_t()` 機構を利用して `_qa_t("no_hit")` に変更し、`SHOIN_LANG=en` でも英語フォールバックが表示されるよう修正。
- **不正な `Content-Length` ヘッダーがサーバーをクラッシュさせる**: `_read_json()` と `_h_src_upload()` で `int(header)` を `try-except ValueError` でガードしていなかった。`"notanumber"` 等の非数値 `Content-Length` ヘッダーが uncaught `ValueError` となり、`ThreadingHTTPServer` のエラーハンドラに達して接続が切断されていた。`_read_json()` では `n=0` にフォールバック、`_h_src_upload()` では `INGEST_EMPTY` エラーを返すよう修正。
- **`export_markdown` が壊れた `citation_report` JSON でクラッシュ**: `json.loads()` を `try-except (JSONDecodeError, ValueError)` で囲み、DB 上の不正 JSON は空の辞書として扱うよう修正。

## [v0.1.24] - 2026-06-13
### Added
- **`qa.py` のユーザー向けメッセージを i18n 対応**: `NO_HIT_TEXT`・`_degraded_text()` のプレフィックス・`SYSTEM_PROMPT`・ユーザープロンプトテンプレートを `_STRINGS` 辞書に集約し、`_t()` ヘルパーで `SHOIN_LANG` に従って選択するよう変更。`SHOIN_LANG=en` 設定時に英語の LLM プロンプトと UI フォールバックテキストが使用される。後方互換のため `NO_HIT_TEXT` / `SYSTEM_PROMPT` はモジュールレベルの日本語定数として維持。未知の言語コードは英語にフォールバック。

## [v0.1.23] - 2026-06-13
### Fixed
- **`GET /api/notebooks` の N+1 クエリ**: `_h_nb_list()` が各ノートブックごとに `store.counts(nb.id)` を呼んでいたため、N 冊のノートブックがある場合に 2N+1 本の SQL が発行されていた。`Store.list_notebooks_with_counts()` を新規追加し、LEFT JOIN + GROUP BY による単一クエリに置換。
- **ノートブック削除時の `questions_cache` メモリリーク**: `_h_nb_delete()` がノートブック削除後にキャッシュエントリを残していた。`_h_nb_clear_chat()` と同様に `questions_cache.pop(nb_id, None)` を追加。

## [v0.1.22] - 2026-06-13
### Fixed
- **`set_embedding()` が存在しないチャンク ID を無音で許容**: `UPDATE chunks SET embedding=? WHERE id=?` の結果を検証しておらず、`rowcount=0`(対象なし)でもエラーにならなかった。`rowcount` を確認し、0 の場合は `CHUNK_NOT_FOUND` を送出するよう修正。`get_source`・`get_chunk`・`get_notebook` 等の他のメソッドと一貫したエラーレポートになった。

## [v0.1.21] - 2026-06-13
### Fixed
- **`reindex_notebook()` が存在しない notebook_id で無音成功**: `store.get_notebook()` を先頭に追加し、存在しないノートブックでは `NOTEBOOK_NOT_FOUND` を送出するように変更。以前は `✓ 0/0 チャンクを再埋め込みしました` と表示して成功扱いになっていた。
- **`shoin notebook list` がノートブック未存在時に無音**: 書院が一つもない場合に何も出力しなかった。`_STRINGS` に `"nb.empty"` キーを追加し、空リストの場合は案内メッセージを表示するよう修正。
- **RIS エクスポートのタイトル・URL フィールドに改行が混入**: ソースタイトルや origin に改行が含まれている場合、RIS 出力のフィールド値が複数行になり RIS パーサが壊れていた。`_ris_escape()` を追加し `TI  -` / `UR  -` フィールドで使用。BibTeX の `_bib_escape()` と同様のアプローチ。

## [v0.1.20] - 2026-06-13
### Added
- **ノートブック削除ボタンを UI に追加**: `DELETE /api/notebooks/{id}` API とi18n文字列 `"nb.delete"` はすでに存在していたが、サイドバーにボタンがなかった。✕ボタンを改名ボタン(✎)の隣に追加し、ホバー時のみ表示(他ボタンと統一)。確認ダイアログ付き。

### Fixed
- **`refreshQuestions` の起動時レース**: `health()` が `loadNotebooks()` より後に解決した場合、`window._llmOn` が `undefined`(falsy)のまま `refreshQuestions()` が早期リターンし、LLM接続中でも推奨質問が表示されなかった。`health()` でLLMがオフ→オン に変わった時(初回チェック含む)に `refreshQuestions()` を呼ぶよう修正。
- **推奨質問のノートブック切替レース**: ノートブックを素早く切り替えると、前のノートブックの質問取得レスポンスが後から届き、現在とは異なるノートブックの推奨質問が表示される可能性があった。`refreshQuestions()` が `await` 前にノートブックIDを捕捉し、応答受信後に `cur.id` と照合して不一致なら破棄するよう修正。
- **`make_report()` に `source_ids` と `source_titles` の長さ不一致チェックを追加**: 長さが異なると `source_id_map` が不完全になり、UIのシール→ソースジャンプが壊れていた。`ValueError` を送出して契約違反を早期検出。

## [v0.1.19] - 2026-06-13
### Fixed
- **`delete_source` が `updated_at` を更新しない**: ソース削除後にノートブックの `updated_at` が更新されなかった。`add_source`・`delete_note` 等では `touch_notebook` を呼ぶのに `delete_source` だけが呼んでいなかった非対称性を修正。`get_source()` が返す `Source.notebook_id` を利用して削除後に `touch_notebook` を呼ぶ。
- **`questions_cache` のスレッドセーフティ**: `ThreadingHTTPServer` 環境で複数スレッドが同時に `questions_cache` を読み書きする際に競合が生じる可能性があった。`threading.Lock` を追加し、`_h_questions` の読み出し・書き込みと `_h_nb_clear_chat` の削除をロックで保護。

## [v0.1.18] - 2026-06-13
### Fixed
- **`add_message` / `clear_messages` が `updated_at` を更新しない**: チャットメッセージの追加・クリア後にノートブックの `updated_at` が更新されなかった。ノートブック一覧を「最近使った順」にソートする場合、会話を重ねてもノートブックが先頭に来なかった。`add_message` と `clear_messages` に `touch_notebook` 呼び出しを追加し、ソース追加・ノート追加と挙動を統一。
- **`_h_src_upload` の生 SQL をストア抽象で置換**: アップロード後のファイル名書き戻しが `store.conn.execute("UPDATE sources ...")` で直接 SQL を実行しており、Store 抽象層を迂回していた。`Store.update_source_title(source_id, title, origin)` メソッドを追加し、`server.py` を書き換え。新メソッドは `get_source` で存在確認後に更新し `touch_notebook` を呼ぶ。
- **推奨質問キャッシュが LLM 障害を永続化**: `suggest_questions` が `LLMError` で `[]` を返した場合、その空リストをキャッシュしてしまい、LLM が復旧しても次のリクエストでは空リストが返り続けていた。ソースが存在するにもかかわらず質問が空の場合はキャッシュしないよう変更し、LLM 復旧後の再試行を可能にした。
- **`_now()` が秒精度だったため同一秒内の `updated_at` 比較が常に等値**: ISO 8601 のタイムスタンプをマイクロ秒精度に変更。既存データとの互換性あり(ISO 8601 文字列の辞書順ソートは精度に依らず正しい)。

## [v0.1.17] - 2026-06-13
### Added
- **ヘルスエンドポイントに `embed_model` フィールド追加**: `GET /api/health` が `{"llm": ..., "model": ..., "embed_model": ...}` を返すようになった。埋め込みモデルが設定済みかどうかをクライアントから確認できる。
- **UI ランプの hover ツールチップにモデル名を表示**: ランプアイコンにマウスを重ねると `LLM: <model> / embed: <embed_model>` 形式のツールチップが表示される。`/api/health` から取得した実際のモデル名を反映し、設定済みの場合のみ表示する。

## [v0.1.16] - 2026-06-13
### Added
- **`shoin reindex <notebook_id>` コマンド**: `SHOIN_EMBED_MODEL` 変更後に全チャンクの埋め込みを現行モデルで再構築できるようになった。`pipeline.reindex_notebook()` が全チャンクを一括取得して `_embed_chunks` に渡し、完了後に `settings["embed_model"]` を更新。CLI は完了チャンク数 `n/total` を表示し、埋め込みモデル未設定時はエラーを返す。`SHOIN_LANG=en` にも対応。

## [v0.1.15] - 2026-06-13
### Added
- **埋め込みモデル変更検出**: `SHOIN_EMBED_MODEL` を変えても旧ベクトルが DB に残り、異なるモデル間の cosine スコアが無意味になる問題を検出できるようになった。マイグレーション #3 で `settings` テーブルを追加し、初回埋め込み後に使用モデル名を永続化。次回起動時にモデル名が変わっていた場合は標準エラーに警告を出力し、再インデックスを促す。`Store.get_setting` / `Store.set_setting` API を追加。

## [v0.1.14] - 2026-06-13
### Fixed
- **`notebook_id` カラムへのインデックス欠如**: `sources`・`notes`・`studio_outputs`・`messages` の各テーブルに `notebook_id` で絞り込む多数のクエリが存在していたが、対応するインデックスがなかった。ノートブックあたりのソース・チャンク・メッセージ件数が増えるほどフルテーブルスキャンが走り遅くなっていた。マイグレーション #2 として 4 つのインデックスを追加(スキーマ変更のため自動適用)。

## [v0.1.13] - 2026-06-13
### Added
- **CLI 国際化 (`SHOIN_LANG`)**: CLI 出力が `SHOIN_LANG=en` で英語に切り替わるように。ノートブック作成・削除・改名・メッセージクリア・引用マーカー・エラー接頭辞を i18n 辞書 `_STRINGS` で管理。UI はすでに `SHOIN_LANG` 対応済みだったが CLI は日本語固定だった矛盾を解消。

## [v0.1.12] - 2026-06-13
### Fixed
- **`updated_at` が note/studio 操作で更新されない**: `add_note`・`delete_note`・`add_studio_output` が `touch_notebook` を呼んでいなかったため、ノートの追加・削除や Studio 出力の生成がノートブックの更新時刻に反映されなかった。`add_source` と挙動を統一。
- **`extract_url` の PDF マジックバイト検出テスト不在**: Content-Type が `application/octet-stream` でも `%PDF` から始まるボディを PDF として処理するコードパスにテストがなかった。モックを使ったユニットテストを追加。

### Changed
- `docs/product-review.md`: v0.1.6–v0.1.7 で実装済みの未実装項目(Studio 等間隔サンプリング、SSE 切断永続化)を実装済みテーブルに移動し、残課題を整理。

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
