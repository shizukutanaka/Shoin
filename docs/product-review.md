# Shoin プロダクトレビュー — 長所・短所・改善案 (v0.2.625 時点、2026-10-01 更新)


shoin/ 16モジュール + 単一HTML UI・テスト1282件・ドキュメント一式の精査結果。

初版は 2026-06-13 (v0.1.0)、全面更新は 2026-07-18 (v0.2.133)、以後は変更のあった項目のみ追記。
初版の詳細な指摘→修正の往復記録は docs/HISTORY.md の Version History(v0.1.37〜、
v0.2.172 で CLAUDE.md から分離)と CHANGELOG.md(〜v0.1.55)にバグ単位で残って
いるため、ここでは現時点の評価に集中する。

## 長所 (維持すべき差別化資産)

1. **引用の機械検証スイート + 出所の全面表示** — 競合に無い核。初版の四段から
   十検査へ成長した: 範囲外番号・字句グラウンディング確認・誤帰属(正しい出典の
   提示付き)・無出典断定(支持文の分割+推定出典付き)・数値一致(位取り漢数字・
   英語数詞・歩合・率・元号を展開)・逐語引用・単位・否定反転・自己矛盾(ターン
   横断含む)・退化ループ(同)。v0.2.130-132 で節文脈、v0.2.228-229 で検出経路
   (全文/意味のどちらが拾ったか)を全出力面に露出し、「何を・どこ・なぜ・どう
   検証」の説明可能性が一貫。
2. **研究裏付けのある検索改善** — コンテキスト付きチャンク(見出しパンくず+タイトルを
   FTS/埋め込みに併走索引, v0.2.123)と マルチクエリ RAG-Fusion(`SHOIN_MULTI_QUERY`,
   DMQR-RAG系, v0.2.125)。どちらも依存ゼロ・オフラインの設計制約内で実装。
3. **依存最小・単一プロセス** — ランタイム依存は pypdf のみ。stdlib HTTPサーバ +
   ビルド不要の単一HTML。
4. **graceful degradation が一級市民** — LLM不在→検索のみ、埋め込み不在→BM25のみ、
   マルチクエリ書き換え失敗→単一クエリ、すべてテストで担保。
5. **SSRF防御の質** — IPピン留めで DNS リバインディングまで遮断(ADR-001)。
6. **日本語一次設計** — FTS5 trigram + CJK対応トークン推定 + かな相互変換 +
   cp932/UTF-16 フォールバックデコード。全角引用括弧 `［Ｓ１］` も UI まで対応。
7. **診断可能性** — `GET /api/health` / `shoin health` / `SHOIN_DEBUG=1` 検索統計。
   設定が「効いているか」をコードを読まずに確認できる。
8. **工学的規律** — 追記専用migration(並行冪等, v0.2.128)、安定エラーコード体系、
   fail-then-pass 必須の回帰テスト文化、UI変更の実ブラウザ(Playwright)検証文化、
   mypy --strict ゼロ。文書の主張とコードの実挙動の突合を繰り返す監査文化
   (v0.2.75/112/113/129 で「文書だけの空約束」を4件検出・解消)。スキーマの
   **ダウングレード安全性も検証済み**(v0.2.158): migration は追記専用・加算的
   (DEFAULT付き列追加/新テーブルのみ)なので、旧バイナリが新スキーマDBを開いても
   `version <= current` 全スキップでそのまま動く。v0.2.164 のノルムキャッシュが一時この性質を狭めた
   (旧バイナリの埋め込み書き込みがキャッシュを陳腐化させ、コサインが定義域外の値になる — 実測)が、
   migration 8/9 のトリガでDB側から構造的に無効化するようにして回復済み(v0.2.167-168)。
9. **XSS安全な UI とアクセシビリティ** — 描画は `textContent`/`createTextNode` のみで
   `innerHTML` 不使用(LLM出力・ソース本文の HTML/スクリプト注入が原理的に無害)。
   全 `aria-label` が i18n 経由(`data-i18n-aria`, v0.2.71)で JA/EN スクリーンリーダー対応。
10. **プライバシー主張が反証テスト済み**(v0.2.158/296) — 「完全ローカル」は宣伝文句ではなく
    列挙で裏付けた事実: 全コードベースの outbound 通信は `llm.py`(ユーザー設定の
    `SHOIN_LLM_URL` のみ)と `ingest.py`(ユーザーが要求した URL、SSRF ピン留め付き)の
    2ファイルだけ。UI の `fetch()` は相対 `/api/…` への1箇所のみで、外部 CDN・フォント・
    アナリティクス等の外部 `src`/`href` 資産はゼロ。ユーザーが指定した先以外へデータを
    送るコードパスは存在しない。ネットワーク面も loopback 限定bind + Host/Origin検証
    (DNSリバインディング/CSRF遮断) + 全応答 no-store + UI CSP で固め、v0.2.296 で
    ファイル面も閉塞——DBは生成時から0600・データdirは0700(`~/.local/share/shoin`が
    共有マシンの他ユーザーにworld-readableだった欠陥を修正、既存installも修復)。
11. **レポートスキーマ規律** — `CitationReport` のキー集合をAST固定し、UI側の
    `report.*` 読み取りも同集合⊆で固定(v0.2.328)。「クライアントが読む形」と
    「サーバが出す形」の不整合クラスを構造的に排除。
12. **トークン会計** — `estimate_tokens` のCJK対応・rank比例 SOURCE_TEXT_TOKENS
    配分(harmonic 1/i + MIN_PER_SOURCE_TOKENS床)・max_tokens切断の `truncated`
    フラグ全面露出(v0.2.245)。
13. **決定性** — 集合反復順のピン(v0.2.469)・全ORDER BYに決定論タイブレーク
    (v0.2.385)・eval `--save`/`--diff` が共有質問集合のみで比較(v0.2.334)。
14. **評価ハーネス内蔵** — `eval` が recall/MRR を単体で計測し、ベースライン
    JSONの往復・diff・missing-source警告まで持つ(v0.2.226-234/551)。
15. **レスポンスヘッダ完備** — nosniff・Referrer-Policy・CSP・Cache-Control:
    no-store・Host/Origin検証・Server ヘッダのバージョン秘匿(v0.2.283-313)。
16. **リクエスト堅牢化** — Content-Length/JSON深さ/アップロード10MB上限、
    accept後のソケットタイムアウト、プロトコルエラーのJSONエンベロープ化
    (v0.2.314-317)。
17. **エラータクソノミー安定** — `*_NOT_FOUND`→404等の接尾辞写像に32件の
    宣言カタログをAST固定(v0.2.465)。CLI/APIで同一のcoded応答パリティ。
18. **i18n対称性** — ja≡enキー・プレースホルダ・`_t`リテラル参照を三方向
    ピン(v0.2.329/337/340-342/349)。サーバ側応答も同一テーブル。
19. **検索の二経路設計** — FTS5 trigram + LIKE fallback が短語・全半角・
    記号混入の全ケースを拾う。MATCH式は全アトム引用でメタ文字注入なし
    (v0.2.471)。
20. **RRF融合** — スケール不整合をrank位置で捨てるCormack式が生BM25/生cosine
    を直接融合。min-max正規化でlexical rerankとの比率も校正済(v0.2.56/189)。
21. **MMR多様化+tail cut** — スコア崖で「関連ゼロの尾」を切除しMMRに
    関連×多様の本質だけ残す(v0.2.59/189)。
22. **PRF(擬似関連フィードバック)** — 語彙不一致を第一パスの上位文書から
    補完する一段expansion。no-extra-callでBM25のみ(v0.2.182)。
23. **用語変種ブリッジ** — かな/全半角/旧字体/元号/漢字骨格/数値略記を
    query側で双方向展開(v0.2.144/213-224)。
24. **否定語検索** — `-term` がASCII単語境界・CJK含意で厳密。negation-only
    クエリはcorpus走査で「X以外全部」を返す(v0.2.238)。
25. **履歴衛生** — 旧[S#]除去・ロール交互強制・per-message 160tok・合計
    400tok・孤児user処理。並行書込み下でも破綻しない順序(v0.1.52+)。
26. **マルチクエリRAG-Fusion** — opt-in環境変数でrewrite+RRF融合。デフォルトは
    単一クエリとバイト同一経路(v0.2.125)。
27. **推奨質問の品質** — 回答不能・重複質問を機械フィルタし、コンテンツ
    フィンガープリント付きキャッシュ(v0.2.260/395)。
28. **Studio出力のライフサイクル** — 5kind・latest-per-kind・失敗再生成で
    既存を消さない・ノートへ保存・superseded剪定(v0.2.373/386/416-417)。
29. **エクスポートの誠実さ** — md/bibtex/risが格納reportを復元して出力。
    RIS行頭タグ偽造不可・BibTeX括弧平衡・legendがprovenanceまで運ぶ。
30. **ノート機能** — ソースとは別レイヤのユーザー注記。detail応答は
    NB_NOTES_LIMIT cap+omitted計数(v0.2.409)。
31. **メッセージAPI** — list/clear・detail埋込みはNB_MESSAGES_LIMIT cap+
    omitted計数(v0.2.250)。
32. **ソースrefresh** — URL再取得・sha256一致ならno-op・pages_failedを
    再取込でも維持(v0.2.243/258)。
33. **取込の広さ** — txt/md/html/pdf/url+file pickerのaccept=パリティ
    (v0.2.375)。
34. **HTMLボイラープレート除去** — nav/footer/formを抽出対象外へ(v0.2.256)。
35. **PDF部分失敗の露出** — pages_failedをingest→API→toastまで端から端へ
    (v0.2.257-258)。
36. **エンコーディング耐性** — cp932/UTF-16フォールバック・gzip/deflate/br
    解凍・単独サロゲート拒否(v0.2.430/438)。
37. **URLタイトル抽出のフォールバック** — og:title→title→最終URLの多段。
38. **チャンク化の質** — 文境界・見出しパンくず・隣接マージ・走査トークン
    計数(v0.2.123/207/290)。
39. **並行モデル** — WAL+busy_timeout+スレッドごとStore+キャッシュ/生成
    ロック。8スレッド×40s混交fuzzで孤児行ゼロ実測。
40. **トランザクション規律** — 複文書込みの`with self.conn:`原子化・
    callee-transacted呼出しサイト・ネストwith禁止を3層AST固定
    (v0.2.419-425)。
41. **防御的decode/degrade** — corrupt BLOB→行スキップ・次元不一致→0.0・
    NaN norm→0.0で「信号なし」へ統一(v0.2.261/630)。
42. **クエリベクトルキャッシュ** — (model,text)の64エントリLRUがロック下で
    mutation-safeコピーを返す(v0.2.121/434)。
43. **接続単一点** — `sqlite3.connect`はstore.pyのみ・PRAGMA一式を
    接続時固定(v0.2.364/444)。
44. **ファイル権限** — DB 0600・データdir 0700を生成時から(v0.2.296)。
45. **healthの二面性** — `GET /api/health`と`shoin health`が同一判定を
    返し、UIランプ/バナーが失敗を反映(v0.2.264)。
46. **loopback限定bind** — serveが127.0.0.1のみへ既定bind、0.0.0.0/::1の
    明示を拒否しないがHost検証でCSRF遮断。
47. **バージョン追跡可能性** — VERSION↔pyproject↔CLAUDE↔HISTORY先頭を
    4点同値ピン(v0.2.357/392)。
48. **文書台帳の実装同期** — spec.md REQ-*のコードトレース・product-review・
    HISTORYをサイクルごとに同期(v0.2.287/295)。
49. **テストピラミッド** — unit+e2e+node-run UI+seeded fuzzの4層が1287件で
    稼働。except-Exception/siteカタログが新規catch-allを構造検出。
50. **fuzzオラクル規律** — 敵対的fuzzの初flagの約9割はoracle側誤りであることを
    記録済。「設計どおりの変換」と「欠陥」を判別してから実欠陥を主張する方法論
    が11回連続で機能。

## 短所 (現存する正直な弱点)

いずれもコードの欠陥ではなく、配送・意図的トレードオフ・カバー範囲の限界である。

| # | 弱点 | 現状 |
|---|------|------|
| 1 | **GitHub Actions CI は未稼働**(自動検証自体は稼働) | `.github/workflows/` は不在で、GitHub App の `workflows` 権限制約は実測再確認済(push が remote rejected, v0.2.153)。ただし「CIをGitHubで回す」は手段であって要件ではなく、本当の要件「landする前に自動検証される」は `scripts/verify.sh` + `.githooks/pre-push` で権限なしに達成済み。`ci/ci.yml` 側も全ゲートを実測し赤くなる3点を修正済み |
| 2 | **PyPI 未発行**(インストール自体は可能・検証済) | `pip install shoin` は実際に失敗する(実測)ため README から除去済み(v0.2.156)。現行案内は clone + `pip install .` で、clean venv からの install→起動→回答まで実機検証済(v0.2.169)。残作業は `twine upload` の認証情報のみ(エージェント不可) |
| 3 | **大規模ノートブックの検索レイテンシ** | ベクトル検索は全チャンクを Python で走査するため件数に線形。v0.2.162-164 で 20,000チャンク 1,489ms → **376ms**(3.96倍)、メモリは件数非依存の O(k) に。文書化上限50,000でも約0.94秒だが、対話的応答を保つなら5,000チャンク程度で分けるのが望ましい(README の Performance 節に明記)。残るのはドット積そのもので、依存ゼロ制約下ではこれ以上削れない |
| 4 | **トークン予算の制約** | 2400トークン総予算・TOP_K=8。長文ソースの切詰め、10ターン超会話の履歴脱落。軽量LLM前提の意図的設計だが限界でもある |
| 5 | **ブラウザ描画の自動テストは無い**(さらに緩和) | レンダリング・レイアウト・イベント配線の回帰はライブ検証のみ。ただし静的契約テスト(`tests/test_ui_contract.py`, v0.2.154)が JS構文・data-i18n の ja/en 完備・UIが叩く `/api/…` のルート実在を恒久カバーし、v0.2.230 で初の**行動的 UI テスト**が加わり、以後の警告・非同期面は実関数をスタブDOMでnode実行して恒久固定する方式へ一般化した: `reportBadges` 連鎖の全フラグ行列(v0.2.235-236、チャットSSE/履歴/Studio 3面)、`openSeal` の同名ソース抜粋照合(v0.2.239)、SSE done 欠落時の永続回答復元(v0.2.246)、`openNotebook` の逆順応答破棄(v0.2.249)、`renderChatHistory` の履歴省略開示(v0.2.250)。v0.2.265-330 で残存全経路まで拡張——タブ列のWAI-ARIAキー操作実装(v0.2.311)、SSEフレームパーサ(v0.2.317)、作成・URL/ファイル追加・ノート・リインデックス・履歴消去・Studio生成・言語切替の全インタラクティブハンドラ(v0.2.318-320)、JS↔Python横断列挙の同値固定(KINDS/export FORMATS、v0.2.322)、viewerのabort guard・フォーカストラップ・遅延details toggle(v0.2.324/326)、ソース行の×削除/rename中ガード/dblclick(v0.2.330——これで全イベントハンドラ完結)、および横断定数の残2件+2契約(COVERAGE_LOW・api()エンベロープ・`report.*`⊆CitationReport・`t()`⊆I18N.ja+ja≡en対称、v0.2.325/328-329)。残る穴は実ブラウザでのレンダリング/操作系で、この限界の実害も2件確認済み: `SHOIN_LANG` が Web UI に効いていなかった欠陥(v0.2.177)と最後のノートブック削除後のエクスポートリンク陳腐化(v0.2.179)は実ブラウザでしか見つからなかった |
| 6 | ~~UIのソース選択が無い~~ → **解消済み** (v0.2.632) | ソース行チェックボックスが `source_ids` スコープへ配線済み。全選択は省略・ゼロ選択はブロック、追加ソース既定ON・明示OFF維持 |
| ~~7~~ | ~~ノートブック横断検索が無い~~ | **解消 (v0.2.649)**: `retrieve`/`bm25_search`/`vector_search` の `notebook_id` を `int | None` 化——`(? IS NULL OR s.notebook_id = ?)` で単一SQL形状のまま全nb横断。`POST /api/search` (nb_id/notebook/title provenance付きhits) + `shoin search <q>` でCLI/Web両面 |
| 8 | **認証・マルチユーザー非対応** | 単一ユーザーの loopback 前提設計。LAN公開するだけで誰でも全ノートブックを読み書きできる(意図的だが、公開用途では致命的) |
| 9 | **TLS 非対応** | stdlib `http.server` ベースで HTTPS を話せない。リモート配置・リバプロ前提の利用は外側の問題として未扱い |
| 10 | **docx/epub/pptx 未対応** | `_EXT_KIND` は txt/md/html/pdf のみ。オフィス文書は前処理で変換が必要 |
| 11 | **OCR 非対応** | スキャンPDFはテキストレイヤが無いと空チャンク化(pages_failed は露出するが内容は取れない) |
| 12 | **画像・表の構造抽出が無い** | HTML img/表は alt テキスト程度まで。図表の意味は検索対象にならない |
| 13 | ~~**CLI ask は非ストリーミング**~~ → **解消済み** (v0.2.635) | `ask()` が `on_delta` コールバックを受理し、chat_stream 搭載バックエンドでは逐字トークンを stdout へ即時転送(連結結果が永続回答と同一のため stdout バイト同一)。chat_stream 非搭載の最小 ChatBackend は従来の一括 chat() 経路へ自動退避 |
| ~~14~~ | ~~同義語・語彙拡張が浅い~~ → **解消済み(v0.2.662)** | term_variants に curated 同義語表(_SYNONYM_GROUPS: 189グループ・カタカナ外来語↔英語・JA↔EN・EN省略形)を追加。norm/casefold/カタカナ/stem の4経路でルックアップし、文字を共有しない 価格↔値段・エラー↔error 級を BM25 leg で橋渡し。open-world(大規模語彙網/LLM生成)は外部依存のため境界外 |
| ~~15~~ | ~~**英語ステミング無し**~~ → **解消済み** (台帳誤記 — `_stem_variants` v0.2.536 で規則活用は橋渡し済: documents→document・running→run・quickly→quick・closed family のみ。ran→run の不規則形は残存) |
| 16 | ~~**ベクトル埋込みのバッチ化なし**~~ → **解消済み** (台帳誤記 — `_embed_chunks` は `EMBED_BATCH=16`/`SHOIN_EMBED_BATCH` のバッチループ `llm.embed(batch)` で既実装。v0.2.648 実測確認) |
| ~~17~~ | ~~**埋込みモデル変更時の自動再索引なし**~~ → **解消済み** (v0.2.661: 不一致がクエリ時stderrヒント+`/api/health`の`indexed_embed_model`/`embed_model_changed`+`shoin health`で可視化・一発修復経路明示。自動再indexは索引全再計算の暗黙mutationのため採らず——静かに死ぬ状態を「可視+actionable」化する境界) |
| 18 | ~~ファイルソースの refresh 非対応~~ → **解消済み** (v0.2.633) | ファイル源は記録 origin パスを再読込。消失済みは `INGEST_FETCH_FAILED`、detail の `refreshable` で UI の↻表示を制御 |
| ~~19~~ | ~~**ソース優先度付けが無い**~~ | **解消 (v0.2.657)**: `sources.weight`(0..8・既定1.0)を融合rawスコアへ正規化前に乗算 (正規化後では床0.0が浮上不能)。w>1優遇・w<1降格・0床固定、`PATCH /api/sources/{id} {weight}` + `shoin source weight` の両面。全往復経路(export/import/trash/merge/duplicate)で保存 |
| ~~20~~ | ~~**ノートブック単位設定が無い**~~ | **解消 (v0.2.659)**: `notebooks.settings`(ホワイトリスト: top_k・source_text_tokens)を migration 13 で追加——k未指定の retrieve 全経路が settings.top_k を解決し、ask/SSE の build_context が settings.source_text_tokens を予算化。`PATCH /api/notebooks/{id} {settings}` + `shoin notebook settings` の両面、全往復経路で保存 |
| 21 | ~~**削除は即物理削除**~~ → **解消済み** (v0.2.654: nb削除が全子表を`trash_items`へJSON undo-log化——同一TXのため巻戻し不能なコミットは構造上存在しない。`GET /api/trash`+restore/purge・`shoin trash`。id衝突はALREADY_EXISTSで拒否=暗黙merge無し) |
| 22 | ~~**DB全体のバックアップ/エクスポート経路なし**~~ → **解消済み** (v0.2.636: `shoin backup <dest>` が SQLite online backup API でライブ一貫スナップショットを作成。dest=0600・`~`展開・自己上書き拒否・coded失敗経路) |
| ~~23~~ | ~~ノートブック統合・複製が無い~~ | **解消 v0.2.656** — 複製 (v0.2.645) に続きmerge側も着地: `Store.merge_notebooks` が元nbの全子表を新規idで取込先へ再挿入 (chunk.source_id・citation_report.source_id_map を id_map で再写像・importと `_insert_tree_rows` 共有) した上で `delete_notebook` 経由で元nbをゴミ箱へアーカイブ=復元可能。`POST /api/notebooks/{id}/merge {"source_id"}` + `shoin notebook merge <target> <src>` のCLI/Web両面 |
| ~~24~~ | ~~**ソースのメタデータが薄い**~~ | **解消 (v0.2.658)**: `sources.meta`(自由JSON・≤4KB)を追加——`PATCH /api/sources/{id} {meta}` + `shoin source meta` で whole-object REPLACE。meta.author/meta.year が BibTeX/RIS の author/AU・year/PY を駆動(取込日≠発行年を修正)。全往復経路で保存 |
| 25 | ~~**全文検索 API が無い**~~ → **解消済み** (v0.2.637: `POST /api/notebooks/{id}/search` が /ask 同一retrieve経路のhitsを生成・永続化なしで返す。`k`は_optional_intで1..50・`source_ids`同一契約・history非展開) |
| 26 | ~~**detail応答のページネーション無し**~~ → **解消済み** (v0.2.646: `GET /api/notebooks/{id}/messages|notes?offset&limit` が cap を超えた全量を newest-first で走査——limit 1..500・total開示・coded検証。sources は cap 対象外のため対象外) |
| ~~27~~ | ~~index.html の単一ファイル化~~ | 解消済み v0.2.666: index.html+app.js+style.cssの3ファイル分割（ビルド不要・リテラルルート配信・CSP script-src self強化） |
| 28 | ~~**ダークモード無し**~~ → **解消済み** (v0.2.638: `prefers-color-scheme` でパレット変数上書き。常時暗帯面は`--band`/`--band-ink`分離、tintリテラル面は個別上書き) |
| 29 | ~~**キーボードショートカットがタブのみ**~~ → **解消済み** (v0.2.642: `/`で質問入力フォーカス・1/2/3でペイン選択。編集中/修飾キー/モーダル中は無効。chat.hintに記載) |
| 30 | ~~**モバイル/レスポンシブ未監査**~~ → **解消済み** (v0.2.641: ≤880px契約を監査・ピン固定。flex input溢れに`min-width:0`、viewer余白8pxへ。レスポンシブ骨格は既存タブ切替) |
| 31 | ~~**印刷スタイル無し**~~ → **解消済み** (v0.2.640: `@media print`でchrome畳込み+3ペイン展開+強制ライト配色。`.msg`はpage-break-inside:avoid) |
| 32 | ~~**ブランディング/テーマ変更機構無し**~~ → **解消済み** (v0.2.643: `GET /api/theme.css`が`~/.config/shoin/theme.css`をverbatim配信——パレットは`:root`変数化済みのため後勝ちlinkで完結。SHOIN_THEME_CSSでパス変更可・CSP 'self'許可) |
| 33 | ~~**バイナリ配布経路なし**~~ → **解消済み(部分的)** (v0.2.668) | `scripts/build_pyz.py` が stdlib `zipapp` で単一ファイル `shoin.pyz` を生成——`python3 shoin.pyz <sub>` で全機能が動作(subprocess e2eピン: `--help`+serve→UI/静的資産200)。zip内アセット読取は `pkgutil.get_data` へ切替(importlibはcapabilityカタログ対象のため不採用)。残存: Python 3.11+ インタプリタは引き続き前提(ネイティブバイナリではない——PyInstaller級はビルド基盤・署名・3OS検証を要しstdlib-only制約と非対称) |
| 34 | ~~**利用メトリクス無し**~~ → **解消済み** (v0.2.653: `Store.bump_metrics`がsettings表へatomic加算(本文非含有・再起動横断)、`GET /api/metrics`/`shoin stats`利用ブロックへ露出。index.ok/fail/ms/embed_skip・ask.count/nohit/degraded/fail/ms。SHOIN_LOG_JSONと対——イベント行vs永続合計) |
| 35 | ~~**LLMエンドポイントの認証非対応**~~ → **解消済み** (v0.2.644: `SHOIN_LLM_API_KEY`設定時に全リクエストへ`Authorization: Bearer`付与——未設定時はヘッダ自体を送らない) |
| 36 | ~~**LLM呼出しにリトライ/バックオフ無し**~~ → **解消済み** (v0.2.639: `_post`が輸送系失敗(TIMEOUT/SERVICE_UNAVAILABLE)のみ指数バックオフで再試行。`SHOIN_LLM_RETRIES`既定2・0-5。chat_stream/availableは対象外——送出済みdeltaの複写防止) |
| 37 | ~~**チャンクの手動編集不可**~~ → **解消済み** (v0.2.647: `PATCH /api/chunks/{id}`/`shoin chunk edit` で in-place 書換え——chunks_au trigger が FTS を同TX再索引・embedding は NULL 降格で reindex 再構築・questions cache 手動evict) |
| 38 | ~~**検索構文のUI露出なし**~~ → **解消済み** (実測確認: `chat.hint`ツールチップ「ヒント: -語 で除外検索」がja/en両言語で実装済み。台帳のみ遅れ) |
| ~~39~~ | ~~ノートブックの共有/受け渡し機構なし~~ | **解消 v0.2.655** — `export --format tree`/`GET .../export?format=tree` が undo-log 同一 envelope(`shoin-nb-tree-v1`)を返し `shoin import`/`POST /api/notebooks/import` が新規idで全行再挿入(chunk source_id・citation_report.source_id_map を id_map 再写像・embedding verbatim・異形文書は `NOTEBOOK_IMPORT_INVALID`) |
| ~~40~~ | ~~API のバージョニング無し~~ → **解消済み(v0.2.663)** | 全応答(JSON/SSE/静的/エラー)に `X-Shoin-API` ヘッダを送出し `GET /api/health` の `api` フィールドでも発見可能に。パスprefixでなくヘッダ駆動——既存クライアント・ルート表・UI契約ピンを無変更に保つ最小侵襲形。互換ポリシーを spec.md へ明文化(additive=非破壊・変更/削除でbump) |
| ~~41~~ | ~~**FTS5 trigram の索引サイズ**~~ → **解消済み** (v0.2.664: 全チャンク書込TX終端で 'optimize' セグメントマージ実行——未マージセグメント蓄積による可避免肥大を構造防止。trigram 3gram展開の固有コストは残存・設計上の境界) |
| ~~42~~ | ~~スペルミス/クエリ訂正なし~~ | **解消 v0.2.650** — ゼロ件時`POST /api/search`・nb_search・`shoin search`へ`suggestions`フィールド追加 (corpus最近接表層形上限3) |
| ~~43~~ | ~~**推奨質問が LLM 依存**~~ | **解消 (v0.2.660)**: LLM 不通時に `_title_questions` がソースタイトル由来の骨格質問(eval --gen と同型・i18n済)を最大n件返す——到達不能が「質問ゼロ」と混同されない。URL酷似/61字超/fold重複タイトルは skip、LLM 応答済みで質問形0行は従来通り[](到達したが何も選ばなかった≠到達不能) |
| 44 | ~~**eval cases の生成支援なし**~~ → **解消済み** (v0.2.651: `shoin eval <nb> --gen` がチャンクを持つソース1件=1ケースの雛形を生成——`sources`は実際のidを運び質問文は手直し前提) |
| 45 | ~~**観測性が SHOIN_DEBUG のみ**~~ → **解消済み** (v0.2.652: `SHOIN_LOG_JSON=1`でJSON Linesイベントログ——`source_indexed`/`ask_completed`がID・件数・ms・degradedをstderr emit。`_PRIVATE_FIELDS`フィルタで本文流出不可。trace層は設計上不要として残) |
| 46 | **Windows 未検証** | 開発・検証は macOS/Linux 前提。`shell=False` 設計とパス処理は概ね移植可能だが実機検証履歴が無い |
| 47 | **Python 3.11 フロア** | requires-python >=3.11 で更に古い distro 標準 Python では動かない(意図的だが利用層を狭める) |
| ~~48~~ | ~~**SSE再接続/再開なし**~~ → **解消済み(v0.2.665)** | delta書込のclient_gone後も生成を完了まで消費し**完全回答**を永続化——トークン消費はserialized lock内で確定済みのため中断は純損。done欠落時のUI回復は基線最終assistant本文との差分で新規行を検出するbounded poll(10×2s)へ拡張しpersist競合で前ターン行を凍結しない。Last-Event-ID再開はPOST+fetch形に標準EventSourceが無く非対象 |
| 49 | ~~**スケジュール/定期タスク機構なし**~~ → **解消済み** (v0.2.648: `POST /api/notebooks/{id}/refresh-all`/`shoin source refresh-all` が全refreshableソースを一括処理——cronに登録すれば定期再取込が完結。per-source結果収集で死んだorigin一つで全体が止まらない) |
| 50 | **ローカルLLM以外の選択肢前提** | 組込みモデル同梱やクラウドAPIキー対応は設計外。ユーザーが別途 OpenAI 互換サーバを用意する必要がある |
| ~~51~~ | ~~**削除undoがノートブックのみ**~~ → **解消済み(v0.2.667)** | ソクラテス監査で発掘: v0.2.654のtrash_itemsはnbのみをカバーし、`delete_source`(upload取込後はtmp origin消失で実質不可復旧)と`delete_note`が永久消失だった。trash_itemsへ`kind`列(migration 14)を追加し両動詞が同TXでアーカイブ——restoreはkind分岐(source=元id+FTS再索引・NOTEBOOK_NOT_FOUND親消失/SOURCE_ALREADY_EXISTS占有、note=新規idで衝突不能)。messages-clearは意図的cleanup+id再採番衝突のため設計上対象外 |
| ~~52~~ | ~~**serve起動が逆引きDNSで~30s停滞**~~ → **解消済み(v0.2.668)** | pyz e2e検証で発掘: stdlib `HTTPServer.server_bind`がbind時に`socket.getfqdn(host)`のPTR参照を実行し、リゾルバ低速/不在環境でlisten自体が遅延(実測35s・ソースツリー同一)。`_HTTPServer.server_bind`をoverrideし`TCPServer.server_bind`+リテラル`server_name`へ置換——ループバック専用に正規名不要(server_nameの消費者はstdlib HTMLエラーページのみで本ハンドラはJSONエンベロープ)。実測 35.0s→0.05s |
| ~~53~~ | ~~**削除済みバイトの物理回収経路なし**~~ → **解消済み(v0.2.669)** | 「蓄積した全バイトは回収されるか」のソクラテス問いで発掘: ①ゴミ箱はpurgeが1件単位で一括emptyの経路が無かった ②SQLiteは削除済み領域をfreelistへ移すだけでDBファイルが物理的に縮小せず、purgeしても消費ディスクが戻らなかった。`trash_purge_all`(1TX全件削除)+`vacuum()`(wal_checkpoint後にVACUUM、before/after/freed報告)+`freelist_bytes()`で回収可能量を`stats`へ可視化。`DELETE /api/trash`+`shoin trash empty`、`POST /api/vacuum`+`shoin vacuum`のREQ-103パリティ |
| ~~54~~ | ~~**破損DBの診断経路なし**~~ → **解消済み(v0.2.670)** | 「破損したDBを診断する経路はあるか」のソクラテス問いで発掘: healthは設定/LLM到達性の面で、「ファイル自体の健全性」の経路がゼロ——corrupt .sqlite はStore()オープンで `sqlite3.DatabaseError: file is not a database` 即死しcoded経路が無かった。`Store.check()`(integrity_check+foreign_key_check+schema版)を新設し `shoin check`(健全rc0/破損・unopenable rc1——cron検知可)+`GET /api/check`(unopenableを200診断値として返す、500潰れなし)の両面へ |
| ~~55~~ | ~~**埋込み欠落が取込toast一度きりで永続不可視**~~ → **解消済み(v0.2.671)** | 「物理健全でも論理欠損は診断できるか」の問いで発掘: 取込時 "N/M embedded" toastのみが欠落を報告し、以後ベクトル脚部分死を発見する経路がゼロ。check()へ `chunks`/`unembedded` 論理層を追加(全面CLI+API)・embed設定時のみreindex修復ヒント。FTS索引desync検出は外部コンテンツ表で原理的不能(rowid走査がcontent表を透過参照)のため非採用——証拠なき防御コードを入れない |
| ~~56~~ | ~~**チャンク数上限がingest経路のみで強制**~~ → **解消済み(v0.2.672)** | 「上限と称する制約は全書込経路で同じ強度か」の問いで発掘: `MAX_CHUNKS_PER_NOTEBOOK`はindex/refresh(pipeline)のみ検査で、import/merge/duplicateの3経路は無検査——大容量export文書や満杯mergeで上限を素通りしベクトル脚が無制限コーパスで全走査。`_insert_tree_rows`(import/merge共有)先頭+`duplicate_notebook`の同TXガードへ `existing+incoming > MAX → INGEST_NOTEBOOK_FULL` で一本化 |
| ~~57~~ | ~~**バイナリがモジバケとして索引される**~~ → **解消済み(v0.2.673)** | 「バイナリファイルはテキストとして索引されてしまうか」の問いで発掘: `_decode`のutf-8-sig→cp932→`errors="replace"`連鎖は**全バイト列を必ず文字列化**するため、.txt/.md名のバイナリが`\ufffd`まみれのゴミチャンクとして索引されBM25/ベクトル脚を汚染(実測50-100%置換)。デコード結果の置換+制御文字密度>20%で`INGEST_BINARY`拒否(file/URL/HTML一点)＋BOM無しUTF-16/32のNUL位置パターン検出で広codec救出(交互NULのロッシー経路を解消)。NUL-only/純置換フォールバックは誠実な再分類で契約追随 |
| ~~58~~ | ~~**「データは端末を離れない」約束が設定で静かに破れうる**~~ → **解消済み(v0.2.674)** | 同問いで発掘: `SHOIN_LLM_URL`が非ループバックを指すと、チャンク本文(embeddings)と質問+コンテキスト(chat)が外部送信されるのに発見経路がゼロ。`endpoint_is_external`(localhost/.localhost/loopback/unspecifiedのみlocal——LAN IP・DNS名はexternal)を新設し、LLMClient構築時のstderr一回警告・`GET /api/health`の`llm_external`・`shoin health`のstderr警告(ja/en)で3面可視化。remote endpointは合法な選択のため拒否ではなく警告 |
| ~~59~~ | ~~**URL userinfo資格情報が表示経路へ逐字漏洩**~~ → **解消済み(v0.2.675)** | 「機密はエラー/診断経路に漏れないか」の問いで発掘: `http://user:pass@host`形のエンドポイントURLが、LLMClient構築時警告(674で追加した自身の表面)・`SYSTEM_SERVICE_UNAVAILABLE`メッセージ(chat/stream両面——SSE/UI/stderr/ログへ伝播)・`shoin health`行へ資格込みで逐字表示。`redact_url_credentials`(authority内の`@`のみ除去——host/port/path/query保持・query内`@`は非資格で温存・malformedでも無raise)を新設し表示のみに適用(リクエストは生URL継続) |
| ~~60~~ | ~~**平文HTTP経路へBearer資格情報が流出**~~ → **解消済み(v0.2.676)** | 「秘密の送信経路は暗号化されているか」の問いで発掘: `http://`+外部エンドポイント+APIキーの組合せで Bearer トークンが平文でワイヤを流れる構成——各要素は合法だが合成で漏洩。`LLMClient.__init__`で3条件揃合時のみstderr警告(https://とloopback httpは免除——TLS保護済/マシンを出ない)。拒否でなく警告(信頼済みLAN構成は合法選択) |
| ~~61~~ | ~~**URL資格情報が永続化・エクスポートへ伝播**~~ → **解消済み(v0.2.677)** | 「取込元URLの資格情報は永続化・エクスポートされるか」の問いで発掘: `shoin add nb http://u:p@host/f` が `sources.origin`(`final_url`)へ資格込み書込み——DB/export/backupへ伝播。fetchはuserinfoを一切送信しない(認証ヘッダ無し)ため資格は機能しない死に重り。`fetch_url`返却+fetch/extractのエラーメッセージ3面でredact(リクエストは生URL継続) |
| ~~62~~ | ~~**HTTPエラー本文が秘密をエコーし得る**~~ → **解消済み(v0.2.678)** | 「サーバー由来のエラー本文に秘密が映り込まないか」の問いで発掘(v0.2.675の記録境界を閉塞): `SYSTEM_LLM_HTTP_ERROR`は応答本文300Bをdetail添付——不良なゲートウェイがBearerキーやURL資格をエコーすると秘密がSSE/UI/stderr/log全面へ伝播。`_post_once`で`llm_api_key()`+`url_userinfo(base_url)`を`***`置換(送信した秘密のみ、host/path温存)。stream経路は本文非読で対象外 |
| ~~63~~ | ~~**同一sha merge/importがraw IntegrityErrorで死ぬ**~~ → **解消済み(v0.2.679)** | 「同一内容のソースを含む2ノートブックのmergeはどうなるか」の問いで発掘: `UNIQUE(notebook_id,sha256)`に裸INSERT——共有sha mergeや文書内dup shaが500化(部分挿入はTX巻戻し)。`_insert_tree_rows`でsha dedupe: 同一sha=同一text=同一決定的chunkのため既存sourceへid_map折返し・chunk INSERT skip・citation_report remap経由で引用解決・cap計算は実挿入数 |
| ~~64~~ | ~~**非正規ファイルが永久ブロックでハングする**~~ → **解消済み(v0.2.680)** | 「ローカルファイルらしきものは全て安全に読めるか」の問いで発掘: `extract_file`は拡張子とst_sizeのみ検査——FIFO/デバイス/ソケットはst_size 0でsize gateを素通りし`read_bytes()`がwriter未到着で永久ブロック(ローカルreadにtimeout不可)。serveでは要求スレッド消費・CLIはハング。`is_file()`で読取前に`INGEST_FETCH_FAILED`拒否——symlinkはfollowして抽出(後者を陽性対照でピン) |
| ~~65~~ | ~~**CLI import が文書を無制限読込**~~ → **解消済み(v0.2.681)** |
| ~~66~~ | ~~**sha dedupeがchunk編集を消失**~~ → **解消済み(v0.2.682)** | Devin Review(#358)発掘: `update_chunk_text`は本文を編集するが`source.sha256`(origin sha)を変えない——編集済みコピーは古いラベルを持ち、v0.2.679の「同sha=同本文」前提dedupeで編集が静かに捨てられた(実検証で消失確認)。dedupeを同一(seq,text)コーパス一致へ厳密化——「同sha・別本文」は文書自身のchunksから再ハッシュ(salt反復)し**両版を保持**。in-document dupも同一経路 |
| ~~67~~ | ~~**trash系deleteのserialize→DELETE間TOCTOU**~~ → **解消済み(v0.2.683)** |
| ~~68~~ | ~~**オフライン時の推奨質問がUIへ届かない**~~ → **解消済み(v0.2.684)** |
| ~~69~~ | ~~**trash系restoreの存在確認probe→INSERT間TOCTOU**~~ → **解消済み(v0.2.685)** |
| ~~70~~ | ~~**nbツリーrestoreがアーカイブidを逐字INSERT——rowid再利用でraw PK衝突**~~ → **解消済み(v0.2.686)** |
| ~~71~~ | ~~**mergeのserialize→deleteが2TX分離——隙間コミット行がarchiveされるがcopyされず零落**~~ → **解消済み(v0.2.687)** |
| ~~72~~ | ~~**source_idsスコープに件数上界なし——数百万idでid毎SELECTループ**~~ → **解消済み(v0.2.688)** | 「数で書ける全ての入力に上界はあるか」の問いで発掘: `_optional_id_list`は要素型のみ検査で件数無制限——~10MBボディで数百万idを送るとask/search両経路がid毎`get_source`をループし要求スレッド上で無制限CPU燃焼。`MAX_SCOPE_IDS=4096`でcoded 400化(実nbのソース数を大きく超過・json_each側も同上限で軽量化) | 「683/685で塞いだcheck-then-actギャップは書込系の残経路にも無いか」の問いで発掘: `merge_notebooks`がsource nbをauto-commit読取(serialize)し、`_insert_tree_rows`のTXコミット後に`delete_notebook`を2段実行——serialize〜delete間の外部コミット行はarchiveに入るがcopyには含まれず、mergeでtargetへ届かない(手動trash restoreでしか回復不可)。単一BEGIN IMMEDIATE化でserialize→copy→archive→deleteを1TXに——crash時も全rollback(旧「duplicate可・loss不可」より強い原子性) | 「id再利用の影響はprobeの範囲内か」の問いで発掘: `_restore_notebook_tree`が全子行のidを逐字INSERTするため、削除後に別insertが`max(rowid)+1`で再利用したidとPK衝突しcodedを経ずraw IntegrityError化(プローブはnb idのみカバー)。子行は`_insert_tree_rows`でfresh rowid化(既存のimport/merge経路と同一ライター)・reportsの`source_id_map`に加え`source_chunk_ids`もremap経由で書換え——`_restore_trashed_source`のchunkもfresh id化(元source契約は維持) | 「683で塞いだcheck-then-actはrestore側にも残っていないか」の対称問いで発掘: `_restore_notebook_tree`/`_restore_trashed_source`/`_restore_trashed_note`のALREADY_EXISTS・NOT_FOUND probeがauto-commit読取で`with`外——probe→INSERT間に外部create/deleteが入るとcoded拒否の代わりにraw IntegrityError(PK衝突)やFK違反(親消失)で生死。全3経路を`BEGIN IMMEDIATE`開始へ——probeがwrite lock下で走り外部writerはbusy_timeout待ち(同型行動ピンで実証) | Devin Review(#358)発掘: `refreshQuestions`が`!window._llmOn`で早期returnするため、LLM不通用に実装された`_title_questions`フォールバック(v0.2.660)がUIでは**完全に無到達**だった——オフラインでchipsゼロ。ガードから`_llmOn`節を除去し端点側フォールバックへ委譲(端点自体がオフライン質問を生成する設計)。node実行ピンを「llm off→fetch+render」へ更新 | Devin Review(#358)発掘: `delete_notebook`/`delete_source`/`delete_note`はpayload SELECTを`with self.conn:`外のauto-commitで読取り——serializeとDELETEの間に別writerがコミットした行はアーカイブ無しでcascade削除され不可逆消失。全3経路を`BEGIN IMMEDIATE`開始へ——payload読取自体がwrite lock下で走り、外部コミットはbusy_timeout待ち(仕込みwriterが50ms timeoutでblockedを実証する行動ピン) | 「上限の無い入力経路は残っていないか」の問いで発掘: API側importは`_read_json`の10MB制御済みだが `shoin import <file>`/`-`(stdin) はread_text+`json.loads`で全量読込——巨大exportでOOM死(coded未到達)。`MAX_IMPORT_BYTES`(1GiB)でstat事前拒否+読取上限+stdin bounded readの3層化。stdinデコードもlocale→厳密utf-8へ(副次修正) |
| ~~73~~ | ~~**source/note restoreの親確認がidのみ——再利用rowidで無関係nbへ誤帰属**~~ → **解消済み(v0.2.689)** | 「id再利用の影響はprobeの範囲内か」の残面(#70の別面)で発掘: 親nb存在確認が`SELECT 1 WHERE id=?`のみ——nb削除→`max(rowid)+1`再利用で無関係nbが同idを得るとプローブを素通りし、削除済みnb由来の子行が誤帰属。アーカイブへ`nb_created_at`同梱+restore時id/created_at組照合で「再利用=親消失」を`NOTEBOOK_NOT_FOUND`拒否化(旧payloadはidのみ検査へフォールバック=undo互換) |
| ~~74~~ | ~~**evalのcases/--diff baseline文書が無制限読込——巨大JSON文書でOOM**~~ → **解消済み(v0.2.690)** | 「上限の無い全量読込は残っていないか」の問いで発掘: `shoin eval`のcases文書と`--diff`ベースラインがread_text全量+無制限json.loads——681のimport閉塞と同じOOM欠陥クラスの残存経路。`MAX_IMPORT_BYTES`を「CLIが一度に読込むJSON文書の上限」へ一般化し両入口をstat+読取の2層で閉塞(拒否はVALIDATION_FIELD_FORMAT_INVALID・文書別ラベル) |
| ~~75~~ | ~~**HTTP同時接続数に上限なし——接続フラッドでスレッド枯渇**~~ → **解消済み(v0.2.691)** | 「寿命が有界でも数が無制限の資源は残っていないか」の問いで発掘: `ThreadingHTTPServer`は接続毎に無制限スレッド生成——`REQUEST_SOCKET_SEC`は各接続の寿命のみ制限し接続「数」は無制限のまま、数千接続の保持でタイムアウト発動前にスレッド枯渇。`MAX_IN_FLIGHT_REQUESTS`(64)セマフォをaccept loopでacquireし過剩をカーネルbacklog滞留へ変換(finally解放・120s有界保持で不デッドロック) |
| ~~76~~ | ~~**import文書がsource id重複を許容——id_map潰れでchunk/reportが別sourceへ無言誤帰属**~~ → **解消済み(v0.2.692)** | 「export→importで文書の整合性は検証されるか」の問いで発掘: 検証は `src_ids` をsetとして構築するため同idの2行を重複として検出せず、細工exportの同id source×2がid_map後勝ちでchunk+reportを最後のsourceへ誤帰属(先行行は0チャンク空殻)——検証ループ先頭のmembership検査で`NOTEBOOK_IMPORT_INVALID`拒否(入口のみ最小・TX未到達) |
| ~~77~~ | ~~**import文書フィールドが書込経路の語彙/範囲/utf8検証を迂回——phantom行の永続化かraw sqlite error(500)**~~ → **解消済み(v0.2.693)** | 「import文書はwriterと同じ検証を受けるか」の問いで発掘: `_insert_tree_rows`のverbatim bindがSOURCE_KINDS/STUDIO_KINDS/role語彙・有限weight・_utf8を迂回——語彙外kind/roleが永続phantom行・Infinity格納・NaN/非str/サロゲートがraw error化。入口検証をwriter契約へ揃え同codedで拒否(_import_str新設) |
| ~~78~~ | ~~**`GET /api/notebooks/{id}`の`sources`埋込みが無制限——detail応答がsource数に比例して永久に重くなる**~~ → **解消済み(v0.2.694)** | 「詳細応答の埋込みリストは全て有界か」の問いで発掘: notes/messagesはv0.2.250/409でcap済みだが`sources`だけ無制限残存——chunk上限は行数を制限するがsource数は制限しない。openNotebook・SSE復帰refetchの全detail取得が蓄積に比例増大。`NB_SOURCES_LIMIT`=2000(scope pane用途で寛容値)+`sources_omitted`開示+`GET /api/notebooks/{id}/sources?offset&limit`走査端点(v0.2.646同型)+UI`src.earlier`行 |
| ~~79~~ | ~~**`GET /api/sources/{id}/text`が全chunk本文を無制限返却——import由来ソースが~1GiB応答を生成可能**~~ → **解消済み(v0.2.695)** | 「全ての読取り応答は有界か」の問いで発掘: detail埋込み(messages/notes/sources)はcap済みだが`/text`はfetchall一括返却のまま残存——import文書はchunk本文長を制限しないため細工exportが1ソースに~1GiB本文を置き得る(viewerクリック毎に二重実体化+64接続でプロセス枯渇)。`SRC_TEXT_BYTES_MAX`(32MiB)上界+境界chunk全体送り+`truncated`/`next_offset`/`total`開示+`?offset=`走査+UI「残り{n}チャンクを表示」導線 |
| ~~80~~ | `GET /api/notebooks`/`GET /api/trash`のリスト応答が行数無制限(同族・アクレッティブ増殖) | v0.2.696 `?offset&limit`ページング+total開示、UIにnb.moreページャー |
| ~~81~~ | cap範囲外sourceは開示のみでscope選択・scoped askが到達不能 | v0.2.697 `src.load_earlier`ページャーで`sources?offset=`追記+scope配線 |
| ~~82~~ | chat履歴・ノートのcap開示も死文(走査端点あり・UI非呼出)+notes開示行が最古側と逆の最下部 | v0.2.698 `wireEarlierPager`3面共用+notes prepend化+messages埋込`id`追加 |
| ~~83~~ | ページャーのfetchがnb切替後にresolveすると別nb配列へ誤merge | v0.2.699 `cur!==target`ガード+`nb.more` isConnectedガード |
| ~~84~~ | exportの複数SELECTが別コミット点を読み同時deleteで破損export化 | v0.2.700 `Store.read_snapshot`で3 export関数を単一WAL読TXへ |
| ~~85~~ | `update_chunk_text`がsha256を動かさず質問キャッシュ指紋不変→編集前テキストのstale質問が無期限配信 | v0.2.701 `questions_fingerprint`がsources行+overview_hits実入力を鍵化 |
| ~~86~~ | `nb.more`ページャーにseen dedup無し→newest-first窓ずれで既表示nbが二重表示 | v0.2.702 seen Set吸収(wireEarlierPager同型) |
| ~~87~~ | `export_notebook`(JSONツリー)がread_snapshot未適用→同時deleteで破損envelopeがimport先へ永続化 | v0.2.703 export_notebookをread_snapshotで包む |
| ~~88~~ | `src_text`ページャーがtotal整合を検証せず→refresh並行で旧先頭+新末尾のtorn表示 | v0.2.704 `p.total !== total`で`src.changed`トースト+splice拒否 |
| ~~89~~ | origin無害化が`_h_nb_import`限定→CLI importでfile origin文書がverbatim保存→refresh経由で任意ファイル読込(LFI) | v0.2.705 `import_notebook`内部へ移設し全入口をsink側防御 |
| ~~90~~ | `src_text`ページャーが`total`のみ検査→in-place編集/同件数replaceでtotal不変のまま新旧テキスト混在(torn残存面) | v0.2.706 migration 15 `content_rev`エポック+`p.rev`比較で不変条件完全カバー |
| ~~91~~ | `_notebook_json`の~8 SELECTがauto-commit連結→同時deleteで埋込リストと`counts`不一致/`omitted`負値 | v0.2.707 `_h_nb_get`を`read_snapshot`で包み単一WALビューへ |
| ~~92~~ | `duplicate_notebook`のdeferred TXでプローブとコピーが別コミット点を読む→同時deleteで子行ゼロの複製がコミット | v0.2.708 `BEGIN IMMEDIATE`+プローブTX内化で単一スナップショット |
| ~~93~~ | chunk上限プローブがpipeline側auto-commit読み→並行ingest/refreshが両方合格し上限越えコミット | v0.2.709 `add_chunks`/`replace_chunks_for_source`をBEGIN IMMEDIATE+TX内cap検査へ |
| ~~94~~ | import文書の`settings`が値検証なしでverbatim格納→`top_k=10**9`/非intがretrieval側で爆発 | v0.2.710 `_import_settings_text`で既知キーを範囲検査(未知キーは前方互換として保持) |
| ~~95~~ | pagerのid dedupが再遭遇行を全スキップ→ページ間のrename/counts変化がリロードまで画面に残る(staleness族第4面: 同一性≠内容不変) | v0.2.711 `Set`→`Map` refresh-merge化(`Object.assign`で可変フィールド更新+nb.moreはDOM行も`replaceWith`再構築) |
| ~~96~~ | メタデータwriterのprobe(get_source/JOIN)がTX外auto-commit読み→A→B間の削除+rowid再利用でwrong-row UPDATE/wrong-nb touch/cap計算 | v0.2.712 5面(update_chunk_text・title・sha256・meta・replace_chunks)をBEGIN IMMEDIATE+ロック内probeへ |
| ~~97~~ | `NB_*_LIMIT`はembed行の件数のみ制限→各行のbodyバイト無制限で~10MB×500行≒5GBのdetail応答(書込1回→全fetchで増幅) | v0.2.713 `MAX_BODY_LEN`をwriter 4面+import document 5面へ coded bound |
| ~~98~~ | #97族の残存面: source `origin`/`sha256`はwriter側`_utf8`のみで無制限→verbatim embed増幅。doc側はtitle/chunk text/context/timestampがunbound(chunk textは全retrievalで全文ロード)、embedding `$blob`とsettings未知エントリもverbatim | v0.2.714 `_import_str`へ一律`MAX_BODY_LEN` bound+embedding blob decode前bound+settings巨大エントリdrop+writer 3面(`add_source` origin/sha256・`update_source_title` origin・`update_source_sha256` sha256)coded拒否 |
| ~~99~~ | doc`meta`が`_meta_text`形状検証のみで`SOURCE_META_MAX`をバイパス→~10MB metaがverbatim永続化し`_source_json` embedで全detail応答へ増幅。settingsもper-entry guardでは合計増幅を止められない | v0.2.715 `_meta_text`内部へ`SOURCE_META_MAX` bound移設(import/restore/将来呼出を1ゲート化)+`_import_settings_text`のper-entry len除去(全オブジェクトboundへ置換) |
| ~~100~~ | `MAX_UPLOAD_BYTES`はファイルbytesのみ制限→PDF内部増幅を制御しない: page objectは~200B/個で数万ページCPU burn・flate解凍で抽出textがファイルの~100倍に膨張し`pages.append`がGB蓄積 | v0.2.716 `MAX_PDF_PAGES=2000`+`MAX_EXTRACT_CHARS=64MB`で`INGEST_FILE_TOO_LARGE` coded拒否(2経路を同一タクソノミーで閉塞) |
| ~~101~~ | `_one_line`適用が部分的→untrusted bytes(stats nb.name・messages body・refresh title・suggest語・report sec・LLM delta/answer/studio/questions・check行・health embed名)がESC/Cf制御系列をターミナルへ素通し(Trojan Source/OSC注入) | v0.2.717 `one_line`/`safe_text`をlog.py集約+Cf追加+残存9経路へwrap(qa警告含む) |
| ~~102~~ | server `_read_json`のみdeep-JSON `RecursionError`を400 coded(v0.2.314) → CLI 3ファイルparse経路(`import` doc・`eval` cases・`eval --diff` baseline)はcatch-allへ転落で~40KBファイルがSYSTEM_INTERNAL_ERROR(500)へ誤分類 | v0.2.718 3 exceptへ`RecursionError`追加(400系coded parity)+20000深度の行動ピン3件 |
| ~~103~~ | `_h_src_text`のbatch loopが1バッチ1 SELECTをauto-commit連結→request途中のchunk rewriteで異コミット行が1応答に混在。rev/total(先頭読み)は旧状態を指しepoch守衛はページ間比較のみ=検出不能なintra-page tear | v0.2.719 `store.read_snapshot()`で全read包み(_h_nb_get同型境界)+別接続replace注入ピン |
| ~~104~~ | `ask()`/search端点のgrounding集合組立て(retrieve legs+settings+context+title/meta lookup)がauto-commit連結の複数SELECT→request途中のingest/replace/deleteで異コミット行が混入。永続化されるanswer+reportが一貫して存在しなかった状態を記述 | v0.2.720 `read_snapshot()`をqa.ask・_h_ask_sse・_h_nb_search・_h_global_searchへ(history/writeは境界外)+注入ピン3件 |
| ~~105~~ | studio/questionsのsampling SELECT群(`overview_hits` sizes+rows・`build_context`再読・`questions_fingerprint`2分割read)がauto-commit連結→request途中のchunk rewriteで永続化されるstudio出力/torn fingerprintが異コミット行を記述 | v0.2.721 `read_snapshot()`をgenerate・suggest_questions・questions_fingerprintへ(LLM呼出/writeは境界外)+注入ピン3件 |
| ~~106~~ | `index_source`の`add_source`コミット→`add_chunks`間失敗で0-chunk孤児source行が永続化——全retrieval不可視のままsource一覧に残り、re-addはsha256 dedupeに衝突し削除しない限り再取込不可(CLAUDE.md記録の既知ギャップ) | v0.2.722 except経路で`delete_source`ベストエフォートrollback+孤児非残存/既存行保全ピン2件 |
| ~~107~~ | `_h_questions`がlookup fingerprint(state A)でcache key化→lookup→generation間のcorpus変更でstate B生成questionsをAのキーへ格納——キーが内容を記述せずstate A閲覧者へAに無い内容のsuggestionsが返る | v0.2.723 `suggest_questions_fingerprinted`が生成snapshot内fingerprintを返却しgen stateでキー化+lockピンregex追随+行動ピン |
| ~~108~~ | `_h_ask_sse`のbudget/context組立てがheaders後の別auto-commitで動作——mid-flight replace/deleteでstreamed回答が旧hits+新context/titleの混在を接地(intra-request tear最終面) | v0.2.724 budget/contextをretrieve snapshot内へ統合——context失敗は_dispatch JSON envelopeへ合流(dangling-guard不要化)+行動ピン4件+カタログ3追随 |
| ~~109~~ | `_title_questions`がfallback時にsnapshot外で`sources_for_notebook`を再読——2読取り間のrenameで新title名指し質問が旧corpusを記述するfpキーでcache(#107のfallback残存面) | v0.2.725 snapshot内の同一sourcesをtitlesとして再利用(第2読取り消去)+`not in titles`死ガード除去+`sources_for_notebook`呼出1回ピン |
| ~~110~~ | multi_query有効時、rewrite(chat)+per-rewrite embed LLM呼出がcallerの`read_snapshot()`内で実行——WAL読取点をネットワーク往復時間分ピン留めし並行writer追加分をcheckpoint不能(WAL肥大化) | v0.2.726 `prepare_retrieval`(LLM相・snapshot外)+`retrieve_prepared`(store相・snapshot内)へ分解、snapshot保持4 callerで相分離+全LLM呼出snapshot外発火の行動ピン |
| ~~111~~ | `source_ids`所属検証がretrieval snapshot外(検証とscoped readが別コミット点のtear——間のdelete+re-addでvalidated idが他nbソースへ再利用され他nb chunk混入)+CLI `ask --source`は検証自体欠落 | v0.2.727 `check_source_scope`をqa.ask/両handlerのsnapshot内へ統合——CLIもqa.ask経由で同一SOURCE_NOT_FOUND契約+get_source全呼出snapshot内発火ピン |
| ~~112~~ | 複数フィールドPATCH(nb name+settings・src weight+meta)が別TX直列で後段拒否時に先段永続化(400応答で部分適用) | v0.2.728 `validate_notebook_settings`/`validate_source_meta`公開helper化+初回書込み前の全フィールド検証——両directionの行動ピン |
| ~~113~~ | embed書込みが`WHERE id=?`のみ——read→commit間のdelete+rowid再利用で旧text由来vectorが別textの新chunkへ誤格納(静かなcorruption) | v0.2.729 `set_embedding`/`_set_embedding_pair`に`expected_text`ガード(`id=? AND text=?`)→rowcount 0→CHUNK_NOT_FOUND→batch rollback・`_embed_chunks` `expected_texts`全呼出必須化(ASTピン) |
| ~~114~~ | ファイル源originが`str(p)`相対パス保存——refreshが実行時cwdで再解釈し、別cwdで失敗 or 同名の別ファイルを無言で再取込(内容の静かな差替) | v0.2.730 `extract_file`が`os.path.abspath`化した絶対origin保存——取込時点の実パスをlocatorとして固定(行動ピン: 相対add→別cwd refresh→byte-identical no-op) |
| ~~115~~ | 子行writer(add_source/add_chunks/add_note/add_studio_output/add_message/clear_messages)の親probeがautocommit実行——probe→write間の親delete+rowid再利用でFKが別親を受理し子行がprobeの見なかった親へ着地(cross-notebook汚染) | v0.2.731 全6 writerのprobeを`BEGIN IMMEDIATE`内へ統一(probeとwriteが同一commit点・第二接続のforeign BEGIN失敗を行動ピン) |

### 解決済み(記録)

| 旧項目 | 解決 |
|---|---|
| `ruff format` 不整合 | ステップ削除で決着(v0.2.153) — 手整形を選んだプロジェクトに従わないスタイルを強制するのは壊れたゲート |
| CHANGELOG.md が v0.1.55 で停止 | 冒頭注記で「正史は CLAUDE.md」と明示・版数非依存化(v0.2.151) |
| 改名後の埋め込み陳腐化 | `pipeline.rename_source()` が当該ソースのみ再埋め込み(v0.2.160) |
| 大きな文書の取込が遅い | `estimate_tokens` の CJK 判定を正規表現+二分探索へ、分割23倍・上限直下で約25秒→3.03秒(v0.2.165) |
| デフォルトブランチが古い版を指す | v0.2.159-181 は main へ着地していたが、**v0.2.182 以降の改善チェーン(125+コミット・114版分)は devin/* ブランチ内にのみ存在**——main は v0.2.181 で滞留中。ロールアップ PR #160 が全シリーズを届ける唯一の経路で、マージ判断待ち |
| 性能特性が未測定 | 取込・検索・引用検証・ペイロードを全実測、README に利用者向けの目安を掲載(v0.2.162-166) |

## 改善案バックログ

**残るのは、いずれも認証情報か管理画面が要る配送操作のみ**(エージェント不可)。エンジニアリング項目はゼロ。

| # | 改善案 | 担当 | 備考 |
|---|--------|------|------|
| 1 | GitHub Actions への CI 移設 | リポジトリ管理者 | `git mv ci/ci.yml .github/workflows/ci.yml`。**二経路で実測拒否**: git push は `refusing to allow a GitHub App to ... without 'workflows' permission`(v0.2.153)、REST API は `403 Resource not accessible by integration`(v0.2.171)。急ぎではない — 要件「landする前の自動検証」は pre-push フックで達成済 |
| 2 | PyPI 発行 | リポジトリ管理者(認証) | ビルド・install・static同梱は検証済。**実測**: `~/.pypirc` 不在、`TWINE_*`/`PYPI_*` 環境変数なし、twine 未インストール。残るは `twine upload` の認証情報のみ |
| 3 | ~~デフォルトブランチ名を `main` へ~~ → **解消済み** | — | 現行のデフォルトブランチは既に `main`(origin HEAD→main を実測確認)。残る課題は上表の「main 滞留」、即ち PR #160 のマージ判断 |
| 4 | リリースタグ / Release 作成 | リポジトリ管理者 | **実測**: タグ push はプロキシが403、GitHub ツール群は release 参照のみで作成APIなし |

いずれも「たぶん無理」ではなく、**このセッションで直接試すかツール面を確認した結果**である。

## 改善点の洗い出し (第一原理 + ソクラテス問答法)

長所50・短所50の棚卸しを、前提と実装の照査へ還元した監査区間 (v0.2.631)。

### 第一原理分解 — この製品の不可約な機能は何か

1. **プライベート文書を取り込む** (ingest/chunk/store) — 唯一の入力。
2. **その文書に限って根拠を引く** (search) — 「どこを読むか」の選択操作。
3. **根拠に接地した回答を生成する** (qa) — 引用付き。
4. **引用が機械検証される** (citation) — 差別化の核。
5. **結果を持ち出せる** (export/UI) — 出力。

問い: 「この機能はどの層に属し、層の必要条件を満たすか」。これに答えられない追加は
YAGNI。短所の多くは「層の外」(認証/TLS/バイナリ配布/telemetry) — 単一ユーザー・
ローカル・依存最小の公理系から見ると、それらは欠陥ではなく**境界の輪郭**である。
残る真の改善余地は層の内側、特に第2層「どこを読むかの選択」——これだけが
ユーザー操作として未提供だった。

### ソクラテス問答 — 前提 vs 実装

| 問い | 前提 | 実装照査 | 判定 |
|------|------|---------|------|
| 「すべてのソースを読む」は必要か | ask は全ソース検索が当然 | NotebookLM はソース選択が中核操作。ノートブック内の特定資料だけに質問する需要は自明 | **未実装 → v0.2.631 で実装** |
| 検索層が「どこを読むか」を表現できるか | `notebook_id` のみが境界 | 全SQLパスは `s.notebook_id` JOIN で済。`s.id IN` の追加は最小侵襲 | 済 (v0.2.631) |
| 範囲外ソースの存在を漏らさないか | 404 と区別されると漏洩 | 他nbのidも `SOURCE_NOT_FOUND` で一律404 | 済 (v0.2.631) |
| UIなしでAPIだけ出すのは中途半端か | UI=操作の唯一面 | API先行→UI後追いの先例あり (env-gated multi-query同型)。SSE契約変更無しで後追い可能 | 後続タスク |
| ノートブック横断検索は必要か | 「資料の集合」はnb単位 | nb横断は関心事の横断検索に使えるが、スコープはsource_idsで7割を満たす | 保留 (設計コスト大) |
| 認証/TLSは必要か | ローカル単一ユーザー | 前提が変われば必須だが、現公理系では層の外。実装すると広範な負債 | 対象外 (設計前提どおり) |
| 依存ゼロを守るべきか | 軽量性の根源 | pypdfのみを維持。ANN index等は依存追加が不可欠 | 維持 (短所3の残りは許容) |
| ファイルrefresh欠落は実害か | `source refresh` がある | **済**——file源も`extract_file(origin)`で再読込(v0.2.633)・`refresh-all`一括経路(v0.2.648)・origin絶対パス化でcwd非依存(v0.2.730) | 済 (v0.2.633/648/730) |
| `shoin stats` (件数/サイズ表示) は必要か | health で概況は見える | detail APIで概ね代替可。優先度は低い | P3 (任意) |
| エクスポートに sources メタ (id一覧) を含めるべきか | スコープ指定はidで操作 | CLI `source list` 相当の表示は今の所 API detail のみ | P2 (将来) |

### 優先順位 (P0〜P4)

- **P0 (今回実装済み)**: `source_ids` スコープ — 第2層の唯一の未提供操作。
  実装規模: SQL層(4サイト) → search → qa → server → cli の縦貫。検証は
  SSE前のcoded応答と全層のスコープピン。
- ~~**P1**: Web UI のソース選択UI~~ → 実装済み (v0.2.632)。
- ~~**P1**: ローカルファイルソースの `refresh` 対応~~ → 実装済み (v0.2.633):
  `refresh_source` が origin スキームで分岐しファイル源は `extract_file` 再読込。
  detail 応答の `refreshable` が↻表示の真値。
- ~~**P2**: `shoin stats`~~ → 実装済み (v0.2.634): テーブル件数 +
  `page_count*page_size` のDBサイズを表示。
- **P3**: ノートブック横断検索 (設計コスト大・需要不確か)。
- **P4**: 残りは層の外 — 認証/TLS/バイナリ配布/telemetry/GHA/PyPI。前提が
  変わったときに再検討。

### 完了・決着済み(記録)

`ruff format` 方針(v0.2.153, 削除で決着) / CHANGELOG 注記(v0.2.151) / 改名時の埋め込み再計算(v0.2.160) / serve 起動ログの i18n(v0.2.161) / Studio 出力への凡例(v0.2.161) / 「監査ラウンドの継続」は成果物でなく習慣のため台帳から削除(v0.2.161, 手順は `docs/agents/opus.md` §5) / Playwright を CI へ = **見送り(判断済み, v0.2.161)**: 安価な8割は静的契約テストで恒久カバー済み、残る2割のためにブラウザDLという重依存を足すのは依存ゼロ原則に見合わない。再検討条件は UI が複数ファイル/ビルド工程を持つようになった時。

## 結論

v0.1.0 時点の結論「設計判断(軽量・検証・日本語の3点集中)は正しく、出荷水準」は
維持どころか強化された。v0.2 系では検索品質(コンテキスト索引・RAG-Fusion・幅/字体の橋渡し)と
出所透明性(節文脈の3面表示)という差別化の本丸を、依存ゼロ制約を守ったまま
研究裏付き実装で伸ばし、50回超の監査ラウンドで並行性・エンコーディング・
トークン推定などの深部バグを fail-then-pass 規律で潰し続けた。

**v0.2.172-179 の要約**(この台帳自体が v0.2.171 で止まっていたため追記): ドキュメント
構造の整理(`docs/HISTORY.md` 分離, CLAUDE.md のセッション毎の注入コスト削減)、
`scripts/verify.sh` 自体の沈黙合格バグの修正(v0.2.174)、新規install での再検証(v0.2.175)、
マルチクエリRAG-Fusionの実機検証(v0.2.176)、そして実ブラウザ検証で見つかった2件の
実欠陥の修正 — `SHOIN_LANG` が Web UI に届いていなかった欠陥(v0.2.177)と、最後の
ノートブック削除後にエクスポートリンクが陳腐化する欠陥(v0.2.179)。どちらも静的契約
テストの死角(実行時の状態遷移)から見つかった、という点で短所#5 の記述を裏付ける
具体例になっている。

**v0.2.381-386 の要約**: 「境界入力の字句契約 + 書込み語彙ガードの確立」区間——
CLIのパス受理面を`~`展開で完全化: `--db`(v0.2.382)、`eval`のcases/`--save`/`--diff`
(v0.2.383)、`add`のtargets(v0.2.384)。引用符付き`'~/x.md'`はシェルのword-start展開
をすり抜けてリテラルで到達するため位置引数も対象。欠陥クラス自体を
`inspect.getsource`走査ピンで封印——cli.py内の全`Path(str(<変数>))`が
`.expanduser()`を持つことを契約化し、将来追加されるパス引数にも契約を強制
(v0.2.383-384)。検索SQLの順位決定性を`, c.id`タイブレークで固定(v0.2.385)——
v0.2.308のlist_notebooks修正と同じ欠陥クラスで、LIKE側はスコアが小整数
(出現回数)のため同点群が常態かつ2000行キャップ境界で同点チャンクが任意に
選捨されていた。書込み語彙ガードを`add_studio_output`へ拡張(v0.2.386)——
typo'd kindが`GROUP BY kind`で幽霊種別として永続化しUIどのセクションにも
属さずエクスポートは無意味見出しで描画する経路を`STUDIO_KIND_INVALID`で
書込み時点拒否。語彙は`store.STUDIO_KINDS`を正本とし`studio.KINDS`は再
エクスポート+assertIs同一性ピン(v0.2.369の`EMBED_MODEL_SETTING_KEY`と同
パターン)でガードと生成語彙の乖離を遮断。v0.2.381はこの台帳自体の同期。
実ガード追加1件・決定性修正1件・ピン/規約強化4件で、role(v0.2.368)→
kind(v0.2.386)の書込み時点語彙検証パターンが確立した。

**v0.2.387-392 の要約**: 「語彙ガード完結・文書契約同期・静かな劣化の表示化」
区間——書込み時点語彙検証の3連を`add_source`で完結(v0.2.387): typo'd kindは
RIS `TY`写像(url/html→ELEC、他→GEN)・md凡例・UIバッジに対して誤った引用
タイプを静かに永続エクスポートする経路で、`VALIDATION_FIELD_FORMAT_INVALID`で
拒否。`SOURCE_KINDS ≡ ingest._EXT_KIND.values() ∪ {"url"}`の双方向ピンが
「新kindが語彙未更新で書込み拒否」「語彙が本番非生成の幽霊値を受理」の両方向を
遮断——この精査で実fixture10件が本番非生成の`"file"`kindを使う潜伏問題を検出・
修正(全て.txtモデル→`"txt"`、幽霊値下のassertは実経路を検査していなかった)。
`docs/spec.md`を62版分同期(v0.2.389)——書込み語彙ガード・エラー体系
(`*_NOT_FOUND`→404/`_ALREADY_EXISTS`→409/`SYSTEM_*`→500/他→400)・
`, c.id`タイブレークを契約文書に反映。意味検索の静かな劣化を表示化
(v0.2.390): 埋め込み設定済みで`n_embedded < n_chunks`(エンドポイント障害・
モデル不一致・部分バッチ)でも取込トーストは「完了」のみ報告していた経路を、
`window._embedOn`(health応答のembed_model=設定有無)条件下で`src.embed_short`
追記へ——LLM無しモードでは0埋め込みが第一級状態のため非表示のまま。配線ピンを
回数一致から「取込完了を告げる全toast行がembedNote(j)を持つ」行単位強制へ強化
(v0.2.391)。バンプ儀式の最後の未ピンマーカー——HISTORY.mdの
`Version History: … → vX.Y.Z`ヘッダ先端——をVERSIONへ固定(v0.2.392)。
実ガード追加1件・UX修復1件・ピン強化2件・文書同期2件で、語彙ガード確立の
区間が閉じた。同時監査クリーン: 空抽出はINGEST_EMPTYでadd_source前に拒否・
messages_omittedはUIマーカーで消費済・studio生成は履歴非消費。

**v0.2.393-396 の要約**: 「状態横断の陳腐化封じ + 防御分岐の実証完了」区間——
残存実バグ1件修正: `questions_cache`のフィンガープリントがソースIDタプルのみで、
**別プロセス**の同一idコンテンツ書換え(`shoin src refresh`等——in-process popの
届かない経路)でsha256/chunkが更新されても一致し続け、死んだチャンク由来の
提案質問を無期限に返していた。`(id, sha256, title)`指紋へ拡張し「供給内容が
変わった時だけ自壊」へ(v0.2.395——失敗方向実証: id-onlyでは陳腐ヒット提供)。
store.pyの最後の未証明防御分岐2件を`_RacyConn`決定的再現で実証(v0.2.394)——
`add_chunks`のFK違反→SOURCE_NOT_FOUND写像と`update_source_sha256`の
トランザクション内再読ガード(兄弟`replace_chunks_for_source`は実証済み、
兄弟対称欠陥クラス)。これでstore.py未達は証明済み到達不可の1行のみ。
evalベースラインの往復忠実性を契約化(v0.2.396)——`--save`→`--diff`で
書込み側・読取り側のキー/型ドリフトが`--diff`実行時まで潜伏し、`"k"`消失は
k不一致警告を**静かに**スキップする実害。v0.2.393はこの台帳自体の同期。
実バグ修正1件・実証2件・ピン1件で、並行性/状態面の監査も漸近化。
同時監査クリーン: 全store getterに明示ORDER BY・UI関数133名がテスト参照済・
fetch_urlはデコード後もサイズ制限・refresh/renameタイトル経路完備・
suggest_questions全フィルタ検証済・export fmtはサーバ400拒否+ライブラリValueError。

**v0.2.397-402 の要約**: 「シャットダウン系の閉塞 + 機構記述≡実機構の照合」
区間——実バグ修正1件: `_HTTPServer`の`daemon_threads`未設定で、ハンドラが
rfile.read()に駐留(REQUEST_SOCKET_SEC=120s)したまま`server_close()`が非デーモン
スレッドをJOIN——Ctrl+Cが実行中リクエスト保持時に最大120s停止(v0.2.398、
python -m http.serverと同選択)。その修正の**前提訂正**が本区間の核心:
`protocol_version`未設定で応答はHTTP/1.0——keep-aliveは存在せず完了GET後の
生存ハンドラ実測0。C226テストの`assertLess(elapsed,2.0)`は変異下でも0.0sを
計測し「検証しない検証」だった。真の駐留経路は「リクエスト途中停止
クライアント」(部分リクエスト行でread駐留)——raw socket再現で変異下2.7sを
捕捉するよう書換え(v0.2.400)、残存keep-alive参照3件を`_drain`の真の存在理由
(未排出bodyでclose→カーネルRST→エラー応答破壊の防止)とstalled-client機構へ
一掃(v0.2.401)。CLIディスパッチ面の最後の未ピン契約を閉塞(v0.2.399):
`add_parser`エントリにdispatch分岐がなければパース成功かつrc=0で**静かに
何もしない**——トップ13件+ネストaction 7名をソーススキャンで固定。
`docs/spec.md`を13版分同期(v0.2.402)。v0.2.397はこの台帳自体の同期。
実バグ修正1件・前提訂正1件・ピン1件・文書同期2件で、「検証したつもり」
クラスも検出対象に入った。

**v0.2.403-405 の要約**: 「ワークフロー指示の実態照合 + 書込み面の局所性封印」
区間——監査はserver.pyのリクエスト処理面をほぼ走査し尽くし、残存欠陥は
「文書が古い手順を教えている」型へ移行。`docs/agents/`の2書がワークブランチ
push後に`git push origin HEAD:main`を指示していた——現在の積層PR運用
(各サイクルが前PR先端に積みbase=先行ブランチで開く)ではmainへの直接pushは
チェーン全体をバイパスし、そもそもこの環境では直接push不可とHISTORYに記録済
だった。両書を積層フロー記述へ訂正(v0.2.404)。恒久ガード1件:
データ変更SQLリテラル(INSERT/REPLACE INTO・DELETE FROM・UPDATE…SET)は
store.pyのみに存在——語彙ガード・updated_at touch・StoreError体系は全て
Storeメソッド内にあり、ハンドラ/パイプラインからの生`conn.execute`書込みは
それら全てを黙ってバイパスする経路だった。読取りSELECTは他層でも合法のまま
(v0.2.405)。v0.2.403はこの台帳自体の同期。文書訂正1件・ピン1件で、
本区間の主な収穫は「教えている手順≡実際の運用」の照合だった。

**v0.2.406-409 の要約**: 「エラー写像の境界完結 + 埋め込み上限の対称化」
区間——実欠陥2件と恒久ガード1件。`urlparse`は`.port`を遅延検証するため
`:abc`/範囲外/負ポートが`fetch_url`内の`.port`アクセスでValueError→500
となっていた(v0.2.45 zone-scoped IPv6と同一の400-vs-500クラス)——
`validate_public_url`内でDNS解決より前に検証し`INGEST_URL_BLOCKED`(400)へ
写像(v0.2.407)。`GET /api/notebooks/{id}`がノートを無制限に全量埋め込み
していた——v0.2.250のメッセージ上限と同一クラスで、ノート蓄積のたびに
openNotebookとSSE切断リカバリ再取得の両方が重くなり続ける経路。
`NB_NOTES_LIMIT=500`で最新を保持し`notes_omitted`を開示、UIは
`chat.earlier`と対称の`notes.earlier`行を表示(v0.2.409)。恒久ガード:
`_read_json()`の結果は`_require()`/`_optional_str()`経由のみ——bound dictの
直接`data.get`/`data[]`は型検証を潜り抜けてlist/dict/boolフィールドが
`.strip()`でAttributeError→500となる経路を、代入した`def`単位のスコープ
スキャンで封印(v0.2.408)。v0.2.406はこの台帳自体の同期。

**v0.2.410-413 の要約**: 「仕様文言の半真区間を実装で解消 + 仕様書自体の
同期」区間——実装1件・ガード1件・文書同期2件。出荷コードの
TODO/FIXMEマーカー0件をスキャンで恒久固定——コミット済みマーカーは
「未修正の既知問題」そのものであり、全ゲート(lint/types/tests/coverage/
secrets)が素通しする完了条件唯一の監査抜けだった(v0.2.411)。REQ-103が
約束する「Studio出力のノート化」は仕様文言のみで実体は手動コピペ
経路だけだった——繰り返し観測された"documented but half-true"クラス
(v0.2.75/112/129/148/173)の残件。`renderStudio`の各カードに保存ボタンを
追加し`{title: 種別ラベル, body: 生Markdown}`を既存POST /notesへ送信、
サーバ変更ゼロで1クリックノート化を実装(v0.2.412)。v0.2.410はこの台帳
自体の同期、v0.2.413は`docs/spec.md`の11版ドリフト解消——SSRF行の
不正ポート→400、DoS行の埋め込みメッセージ/ノート上限+*_omitted開示、
書込みSQL局所性・バリデータ契約・ゼロマーカーピンを仕様書へ折込。

**v0.2.441-444 の要約**: 「セキュリティファンネルの所有点
封印——verb経路と接続生成の単一点化」区間——構造ピン2件・
文書同期2件。DNS-rebinding/CSRFガード(`_reject_cross_site`)は
`_dispatch`内でのみ実行されるため、ファンネルを通らない
`do_*`メソッドの追加(将来のdo_HEAD/do_PUT等)はガードを
バイパスした応答を返しつつ全テストを素通りする——AST走査で
全verb本体の`self._dispatch(...)`呼出しを必須化(v0.2.442)。
`sqlite3.connect`はstore.pyのみが所有(row_factory/WAL/
foreign_keys PRAGMA/0600パーミッション)するが、store外への
接続サイト追加はFK=OFF・journal=DELETE・デフォルト権限の
接続を静かに生成する——AST走査でshoin/全体を固定(v0.2.444)。
v0.2.441/443は台帳・仕様書の同期。

**v0.2.445-448 の要約**: 「TX契約のcaller側完結 + 欠陥隠蔽
経路のカタログ封印」区間——構造ピン2件・文書同期2件。
TX制御動詞(.conn.commit/rollback/executescript/executemany)の
store外使用はpipeline.py(`_embed_chunks`バッチTX契約)のみ
許可——v0.2.405のSQLスキャンは文面のみ見るためTX呼出し自体は
未監視で、ハンドラ中の新規`.conn.commit()`がcalleeのpending
書込みを早期確定させるcaller側変種を封印(v0.2.446)。
`except Exception`広域捕捉も同型ホール:既存12サイトは全て
意図文書化済みだが(ruff BLE001は警告止まりのため)新規の無分類
catch-allはlintを素通りして該当欠陥クラスを静かに隠蔽する
——per-file件数カタログで固定し、新規サイトはテスト更新+
自身の根拠文書を必須化(v0.2.448)。v0.2.445/447は台帳・
仕様書の同期。

**v0.2.449-452 の要約**: 「無期限待機と無監視共有状態——
リソース境界の宣言化」区間——構造ピン2件・文書同期2件。
ネットワーク呼出し(urlopen/create_connection)の全サイトに
timeout明示をAST強制——無指定は無期限待機でハンドラスレッドを
占有し、per-requestスレッドが枯渇へ蓄積する経路を封印
(v0.2.450)。モジュールレベルの可変コレクションを宣言済み
(file,name)集合に限定——唯一の真可変`_QUERY_VEC_CACHE`は
`_QUERY_VEC_LOCK`下で、新規の無ロック共有可変は単一スレッド
テストを素通りして並行競合する経路を封印(v0.2.451)。
v0.2.449/452は台帳・仕様書の同期。

**v0.2.453-456 の要約**: 「コード形状の危険面——プリミティブ・
双子経路・正規表現ジオメトリの走査封印」区間——構造ピン3件・
台帳同期1件。eval/exec/compile/`__import__`/globals/locals呼出しと
pickle/marshal/subprocess/ctypes/code/ptyのimport+呼出しサイト、
及び可変デフォルト引数をAST禁止——stdlib-onlyツールで正当化
不能な注入シンクと全呼出し共有エイリアシング欠陥を現状ゼロの
まま恒久化(v0.2.454)。同ピンの範囲穴を即修復——import禁止は
`os.system`/`os.popen`/`os.spawn*`/`os.exec*`/`os.startfile`を
正規モジュールrootの下で見逃していた双子経路をexec/spawn属性族
としてフラグ化(v0.2.455)。正規表現面はAST+sre_parse走査で壊滅
ジオメトリ（無制限repeat内の無制限グループ・先頭重複
alternation）を固定し、非リテラル`re.compile`10サイトを
カタログ化——定数テーブルalternationか`re.escape`補間のみの
構築規律を宣言化(v0.2.456)。v0.2.453は台帳同期。

**v0.2.457-462 の要約**: 「呼び出し面の完結——`python -m`経路の修復と
catch-allバイパス3経路の閉塞」区間——実欠陥1件・構造ピン2件・
文書3件。console_script経路(`shoin`コマンド)はインストール後のみ
存在するため、ソースツリーからの直接実行`python -m shoin`は
`No module named shoin.__main__`で失敗していた標準invocation規約の
欠落を修復——3行の`__main__.py`で`cli.main()`へ委譲し両経路を
永遠にドリフト不能化、フレッシュインタプリタでの`-m shoin --help`
e2eテストも付帯(v0.2.459)。except-Exceptionカタログが見えない
3つの同型バイパス経路を同一テスト内で閉塞——裸`except:`・
`except BaseException`・`contextlib.suppress(Exception/BaseException)`
(KI/SystemExitも呑込む、またはCM経由の同一静寂呑込み)。
既存`suppress(OSError)`は狭域のため許可維持＋非真空チェックの
アンカー化(v0.2.460)。宣言`requires-python >=3.11`と実構文の乖離
を恒久封印——devが3.12インタプリタで緩和構文(同一引用符ネスト
f-string・`type`文)が静かにコンパイルされフロアユーザーで初回実行
クラッシュする経路を、全ファイルの`ast.parse(feature_version=(3,11))`
リプレイで遮蔽(v0.2.461)。READMEへ`python -m`代替経路の発見可能
1行を追記(v0.2.462)。v0.2.457/458は台帳・仕様書の同期。

**v0.2.472-474 の要約**: 「stdout契約の双子経路閉塞と初リリース
タグ」区間——構造ピン1件・文書2件・運用1件。v0.2.470の
stdoutピンを`print()`走査では見えない2つの同欠陥クラスへ拡張:
cli.py外の全`sys.stdout`属性アクセス(write/再代入はprintを迂回
しつつ同ストリームを汚染)を全面禁止、`import logging`/`from
logging`のshoin/内使用を禁止(diag規約はstderr print——未設定
loggerの出力はlastResort stderrまたは沈黙、stdout配線ハンドラは
全クラスを再開)(v0.2.474)。v0.2.472/473は台帳・仕様書の同期。
運用面: 蓄積チェーン全着地を経てmain実測で全ゲート検証のうえ
リポジトリ初のリリースタグ`v0.2.474`を発行(2026-10-01)。

**v0.2.467-471 の要約**: 「出力面の機械可読契約——ヘッダ・順序・
ストリーム・クエリ言語の四象封印」区間——構造ピン4件・文書1件。
`send_header`/`_headers`の値引数を全AST走査し定数/`str(len)`/
ルート整数・閉マップ参照/`safe_lang`/安全f-string以外を拒否——
将来ノート名等をヘッダへ流す変更はCRLFインジェクションか情報
漏洩になる構造ホールをホワイトリスト化(v0.2.468)。set反復の
PYTHONHASHSEED順序非決定性がappend/extend/yield/list/tuple/join
経由でJSON・エクスポート・フラグリストへ焼き込まれる残クラスを
消費側ASTピンで閉塞——`sorted()`のみが順序付け経路の慣行を明文化
(v0.2.469)。stdoutは機械可読契約(`shoin eval`/パイプ先構造化出力)
——ライブラリ層`print()`の混入はどのテストもストリームを
アサートしないため不可視、cli.py以外の全`print()`に`file=sys.stderr`
を強制し`serve()`バナーのみ例外許可(v0.2.470)。FTS5 `MATCH`は
独自クエリ言語——引用符なし補間は`x:y`/`"`でWHERE意味を例外なしに
改竄するため、全アトム二重引用を行動検証＋`MATCH ?`サイト1件を
カタログ固定(v0.2.471)。v0.2.467はこの台帳自体の同期。

**v0.2.596-605 の要約**: 「防御機構内部の一貫性——半修正の完結と
出力境界の衛生化」区間——実欠陥7件・文書同期3件(v0.2.596は本台帳、
v0.2.597はspec.md、v0.2.605はagent-doc訂正)。主題は「守る側の機構
自体が契約を半分しか満たしていない時」: `json.loads`の`\ud800`
エスケープで物質化する単独サロゲートがeval両リーダーを通過しsqlite
bind/encodeで生クラッシュ——`_utf8_ok`でUTF-8往復を要求(v0.2.598)。
LLM出力の同欠陥は**全下流**(message/parts/stream-delta + 中毒化する
questions_cache: クラッシュ前に書込されるため永続500)——デコード境界
全出口に`_strip_surrogates`(v0.2.599)。`_embed_chunks`のLLMError枝は
`except Exception`兄弟と違いrollbackを欠落——v0.2.419 pending-tx
leak族の最後のsiblingを閉塞、失敗バッチの部分ベクトルが後続コミットで
静かにflushされ旧モデル名マーカーのまま新モデルベクトルが混在する経路
(v0.2.600)。`_segment_claims`のlead+trailing同一S出現が5検査全てで
lead短絡——segment出現の誤帰属/捏造数値/否定反転が不可視(false
silence方向)をunion評価で修復(v0.2.601)。`rrf_fuse_lists`のvecは
last-wins・bm25はfirst-wins——マージ規約の非対称でrewriteの弱い
コサインがprimaryの強いシグナルを上書き(v0.2.602)。`_SKIP_TAG_BALANCE`
中和機構はcloserを1個だけ注入——2-open未対応タグで`_skip_depth`が
残存し文書残部を飲込(機構が防ぐはずのクラスが半修正)、注入数を
opens−closesへ(v0.2.603)。`_HEADING_RE`はfenceと異なり0空白必須+
末尾空白必須——CommonMark opener規則(≤3空白+#+空白/EOL)へ統一、
indented/bare headingがbreadcrumbとheading加重BM25へ届く(v0.2.604)。
全区間fail-then-pass規律で実証済み。


**v0.2.606-611 の要約**: 「単独サロゲート欠陥クラスの完結——
入出力境界全ての衛生化」区間——実欠陥4件・文書同期2件
(v0.2.606は本台帳、v0.2.608はspec.md)。主題はv0.2.598-599で
開いたサロゲートクラスを残りの全出力境界で閉塞する収束: store層が
sqlite3のstrict UTF-8 bind拒絶を無防備に通していた——`_utf8`
モジュールゲートが全バインドstrフィールド(notebook name・source
title/origin/sha256・chunk texts/contexts・note/studio/message
body+report・settings key/value)を書込み前検査し
VALIDATION_FIELD_FORMAT_INVALIDを返す第4境界(v0.2.609)。
`_sse`は`ensure_ascii=False`+`.encode()`で、サロゲート入り
ペイロードがConnectionError捕捉を潜り抜け`_dispatch`の500書込みへ
流出——コミット済みSSEボディへの第2HTTPステータス行注入を
ensure_ascii=Trueで閉塞(ワイヤ上ASCII純粋・\ud800エスケープで
クライアント側JSON.parseが透過復元、v0.2.610)。cli `main()`の
例外分類はUnicodeEncodeErrorを捕捉せず——カスタムChatBackend
(`main(llm=…)`/`make_server(llm=…)`拡張点)のサロゲートトークンが
print()書込みでクラッシュし生トレースバック、OverflowErrorと同じ
境界捕捉でerr.prefix化(v0.2.611)。併せてexportステータス行の
uncited_supportedヒントが非空ターゲットを要求し、古いレポート形状
での宙吊り矢印を解消(v0.2.607)。


**v0.2.612-617 の要約**: 「seeded-fuzzが拾う契約反転——
『足すだけ』系不変条件と寛容パーサーの下流穴」区間——実欠陥3件・
文書同期2件(v0.2.612は本台帳、v0.2.616はspec.md)。全モジュール
1周監査の完結後、第2フェーズとしてseeded-random fuzzが読みでは
拾えない契約の反転を検出: `bm25_prf_search`は第1パス+拡張ヒットの
unionをbm25再ソート後`[:k]`で切詰——第1パス未充填時、システム提案
gramのみ一致の密な拡張ヒットがユーザ用語一致ヒットを退避させ得る
「expanded hits can only ADD recall」契約の反転。extrasを
`k - len(hits)`ヘッドルームに上限(v0.2.613)。`_json`応答writerは
`.encode()`がサロゲートpayloadでraise——エラーエンベロープ経路が
HTTP応答そのものを失う経路をensure_ascii退避で閉塞(v0.2.614)。
`html_to_text`修復パスは「パーサーが実際にイベントを発火しない領域」
のタグ/コメント境界をペア——`_skip_depth`盲目カウンタを`_skip_stack`
名スタック(DOM意味論pop-through+未開クローザー無視)、`<...>`属性領域・
コメント・CDATA内イベントを`_live`述語で全消費点から除外、注入修復を
per-opener化する3機構で一括閉塞(v0.2.615)。`_parse_report`は任意
well-formed JSONを受理する寛容設計なのに読取3箇所が格納形状を無条件
信頼——`_legend`の`found_bits(source_detail値)`非dict AttributeError・
`_status_line`の`set(cited)`/`sup_src.get(文)`の非hashable
TypeError——1行の変形レポートがexport文書全体をクラッシュさせた経路を
「変形フィールド→no-signal」降格で閉塞(v0.2.617)。store層並行書込み・
pipeline→build_context統合・実サーバ960リクエスト嵐の各fuzz経路は
全てゼロ欠陥——`with self.conn`原子性・WALスナップショット・coded
error envelope・SSE wire完全性の契約が嵐下で維持されることを実証。


**v0.2.618-621 の要約**: 「stdlib境界の非コード化漏出——
400-vs-500欠陥クラスの第3波を機械検出」区間——実欠陥3件・
文書同期2件(v0.2.618は本台帳、v0.2.621はspec.md)。同じ
「悪意/変形入力がstdlibの想定内エラー型ではなく別型で境界を
突破する」クラスが2モジュールで連続検出された:
`urlparse`は括弧付きホストをパース時点で検証するため
`http://[::1`の未閉ブラケットが裸ValueErrorとして`.port`
遅延チェック以前に脱出し500化(v0.2.45と同系譜)——
`INGEST_URL_BLOCKED`へ写像。`_decode`のcharset候補loopは
NUL混入charset名(敵対的Content-Typeヘッダ経由で到達)で
codec lookupが`LookupError`でなく`ValueError("embedded
null character")`を送出してfallbackを抜ける——catch節を
`(ValueError, LookupError)`へ(v0.2.619)。`json.loads`は
~5k超深ネストbodyでJSONDecodeErrorでなくRecursionErrorを
送出——`_post`は全呼出し経路の500化、chat_streamは1枚の
深い`data:`フレームがSSE全体を途中abortさせる両漏出を
BAD_RESPONSE写像+malformed-frame dropで閉塞(v0.2.620)。
**設計含意**: 「リモート/ユーザ制御データを渡すstdlib境界」は
単体で監査面を構成する——各境界が送出し得る例外型の全列挙が
必要であり、exceptインベントリカタログがこのクラスの再発を
構造検出する。pipeline.py refresh/rename/reindex経路の
seeded-fuzz(409試行・cap境界delta走査・embed入力同一性
oracle)はゼロ欠陥——同一sha no-op・SOURCE_ALREADY_EXISTS・
NOTEBOOK_FULL算術・部分force-reindexのembed_model非記録の
全不変条件が維持されることを実証。


**v0.2.622-625 の要約**: 「出力面と予算の構成保証——境界そのものでは
なく境界の『組立て方』の欠陥」区間——実欠陥2件・文書同期2件。
CLIの単一行ラベル(`✓`/`✗`・`[id] name`・`[S{n}]`行)は外部制御
文字列を生で埋込んでいたため、`\n`入りターゲット名で行が分裂し
偽`✓`行を、ESC系列で前行の上書きを許した(端末出力インジェクション)
——Cc/Zl/Zp文字を`\\n`/`\\xNN`/`\\uXXXX`へ変換する
`_one_line`を全16埋込サイトへ適用、複数行が正当なブロック内容は
対象外(v0.2.623)。`_hard_split`の最終手段char-windowは窓幅を
全体の平均トークン密度で逆算していたため、混合密度テキストの
高密度ポケットでlimit超過窓を放出(実測675@512)——estimateの
prefix単調性で最長適合prefixを二分探索する`_window_split`へ
置換し全ピース≤limitを構成保証(v0.2.624)。
**設計含意**: 「境界が個々に正しくても、境界を組立てる算術が
近似を積む欠陥族」——平均×固定strideは局所ピークで破れる、
型安全だが意味を変えるエスケープ不在の埋込みは信頼記号の偽造を
許す。seeded-fuzzのクリーン面: server.py残存GET経路(~470req・
エンコード数字/巨大id/format変異)、evaluate.py統合(~20k試行・
parse/baseline/diff全てValueError契約保持)はともに欠陥ゼロ——
初期flagは全てoracle側の契約誤認(multiset差分vs契約上のset
membership)で、参照実装の設計通り動作。


**v0.2.586-595 の要約**: 「入出力境界の防衛深化——想定外入力を
コード化契約へ写像する層の閉塞」区間——実欠陥9件・台帳同期1件
(v0.2.586は本台帳のみ)。主題は「層をまたぐ入力が想定形状を外れた時、
その層の契約を保持するか」: pypdfはページオブジェクトを遅延解決するため
`reader.pages[i]`のmaterialize自体がcorrupt xrefでraise——extract_text
未到達の生例外をページ単位失敗へ降格(v0.2.587)。`_blocks()`が
フェンス内の`# コメント`行をATX見出し・空行をブロック境界と誤認し、
コードコメントがbreadcrumbへ混入——CommonMark規則(opener/closer対称・
info文字列排斥)でフェンス状態を追跡(v0.2.588)。`refresh_source`の
sha一致no-op経路が抽出済み`pages_failed`を0へ捨てる信号損失
(v0.2.589)。`rewrite_queries`のdedup fold keyがcap切り捨て**後**の
排出形でなく全文で計算——cap跨ぎ差異が2スロットを同一テキストで消費
(v0.2.590)。`replace_chunks_for_source`のtitle経路のみstrip+空拒否を
欠落——3兄弟writerの規約を第4経路へ移植(v0.2.591)。`_h_ask_sse`の
token生成ブロックがLLMError/ConnectionErrorのみ捕捉し、TimeoutError等が
SSEヘッダ確定後にgeneric-500として第2ステータス行を本文へ混入＋
assistant行を孤立化——coded-vs-generic方針のerror frame+永続化保証へ
(v0.2.592)。`_cmd_eval`両読込でUnicodeDecodeErrorが全ハンドラを潜り
抜け非UTF-8入力で生traceback——JSONDecodeErrorとのタプル捕捉で
VALIDATION_FIELD_FORMAT_INVALIDへ(v0.2.593)。`Request()`構築は
コンストラクタでurlsplitを走らせるため、unclosed IPv6ブラケットが
urlopen実行前にraise——`_post`/`chat_stream`の2サイトを`available()`
と同じtry内構築へ(v0.2.594)。`json.loads`は非標準NaN/Infinity
リテラルを受理しboolはintサブクラスのため、`isinstance`のみでは
`{recall: NaN}`がdiff算術へNaN伝播——全5数値フィールドへ有限+非bool
要求(v0.2.595)。全区間fail-then-pass規律で実証済み。

**v0.2.575-585 の要約**: 「引用マーカー帰属規約の断片単位統一——
[S#]記法の名空間一意化」区間——実欠陥10件・台帳同期1件(v0.2.575は
本台帳+_FENCE_RE修複)。主題は「マーカーはどのclaimを所有するか」の
断片意味論: splitterが`'claim。[S1] next。'`を`'claim。'`+`'[S1] next。'`
へ割るため、断片先頭マーカーの帰属が全検査で反転——uncited_sentencesが
マーカー本体を自己被覆と誤認し直前claimをフラグ(v0.2.576の二重反転)、
verify_grounding等6検査も先頭runを自断片で評価し正引用をmisattributed
誤告発(v0.2.577で`_leading_markers`→prev_claim経路へ統一)。
断片末尾の未被覆面も不可視だったため最終マーカー以降を評価対象化
(v0.2.578)。`_segment_claims`は同一S番号の2出現を後節で上書き——
第1節が全検査対象外かつ正当出現が誤転落、dict→出現順listへ
(v0.2.580)。沈黙原則側の3件: `A[S1]によるとB`中断片熟語は帰属が真に
曖昧なため断片ごと沈黙(v0.2.583)、`[S1] によると`の空白形状が熟語
マッチを破り誤転落していたのを`^\s*`許容で修復(v0.2.582)、
`_DISCLAIMER_MARKERS`が定型免責フレーズ9形状を見逃し正しい
「記載なし」応答を誤フラグ——幹形+casefold+領域名詞必須で拡張
(v0.2.579)。`self_contradictions`数値腕はdifflib最小化span(50%→30%
が'5'→'3')上で走り全1桁swapが死域——ops==1保証下の全文数値集合比較へ
(v0.2.581)。`retrieve_multi`のexp印をvector脚へ対称化——rewrite埋め込み
のみで回収される意味的ヒットがlex==0切断対象だった(v0.2.584)。
export_markdownの`## ソース`列挙が`[S#]`記法を流用し同一文書内で
「notebook並び順」と「retrieval順位」の2名空間を衝突——番号付き
リストへ変更し引用記法をsource_map凡例専有化(v0.2.585)。
全区間fail-then-pass規律で実証済み。

**v0.2.566-574 の要約**: 「数詞構文の実文法化——列挙と位置表記を
『部分は主張しない』一規則へ統一」区間——実欠陥6件・メタ監査1件
(v0.2.566は本台帳・v0.2.567はfold冪等性修復)。主題はシード乱数
ファズによる数詞族の系統的欠落摘出: `_numbers_expanded`の接尾辞対鎖が
「億/万/兆区切りの位置表記」を構造的に読めず'一万二千三百四十五'→
{12000,2345}と真値を欠落させ、素run経路が群成分を漏出——実位置文法
(seg BIG_MAG)+ seg? へ書換え、群はトークン値の成分のみを主張し
silence契約を完全保存(v0.2.568)。往復ファズ`_int_to_kanji`全定義域
(6万+値)で裸run spanの成分漏出~8000件を検出——'二千一'→{2000,2001}
——3span族全てを同規則へ統一(v0.2.569)。`_conv_values`の同一単位系
チェインがgap≤2文字の任意文字で合算し'1時間、30分'→90分の偽メンバーが
真フラグを抑制——加算結合は空白と'と'のみへ限定(v0.2.570)。
`_en_value`が連続small値を文法外加算('one two'→3,'fifteen two'→17)
——tens+unit文法へ状態機械化、列挙は漢字'一二三'と同じ沈黙(v0.2.571)。
`_stem_variants('news')`が不変化不可算名詞から生きた高頻度誤語'new'を
OR変種へ注入——死語形許容範囲を逸脱する語彙的例外として`_STEM_INVARIANT`
へ明文化(v0.2.572)。検索側2件: `bm25_search`早期リターンのカバレッジ
判定が`_numeric_query_terms`を見ず、CJK-coveredクエリ'五割の回答者'で
展開値'50'(len<3)のLIKE針が不発——検査対象を同じ項集合へ統一
(v0.2.573)。`_norm_query_terms`も同項を見ず、ブリッジ回収チャンクが
lex=0.0→`_tail_cut`でterm-free切断——v0.2.539の「retrievedなのに
term-free」規則の残存族を同じ対称で閉塞(v0.2.574)。全区間
fail-then-pass規律で実証済み、残監査面は深読クリーン確認のみ。

**v0.2.559-565 の要約**: 「台帳と監査網の相互同期——文書の散文主張を
コード計測へピン化」区間——実欠陥2件・メタピン2件・文書同期3件
(v0.2.559は本台帳・v0.2.560はspec.md・v0.2.563はspec件数修正)。
主題はドキュメント層への監査網拡張: 定期同期設計のためVERSIONへ
遅れるのが正常なラグ型マーカー(spec.md「実装 vX.Y.Z 時点に同期」・
product-review「vX.Y.Z 時点」)に唯一許されない形状——出荷版を超える
版数の主張——を`test_doc_sync_markers_never_exceed_version`で上限
固定(v0.2.561)。spec.md散文のハードコード件数(except-Exception
カタログ)が実測13に対し「12サイト」のまま約60版生存したのを修正
(v0.2.503のraw socket close追加時に記述側が未追従——git log -Sで
帰属特定・v0.2.474タグ時点では正値を検証)(v0.2.563)し、再発防止
として3カタログをモジュール定数へ昇格+`test_doc_catalog_counts_match_spec`
で散文件数≡実カタログを照合(v0.2.564)。実欠陥2件: `report_from_dict`
は`missing`のみ要素int検査(bool除外済)で`expected`/`retrieved`は
リスト型のみ——同一検証ブロック内の兄弟idリストへ厳格さを統一し、
手編集ベースラインの`["1"]`/`[true]`/`[1.5]`が静かに往復する経路を
閉塞(v0.2.562)。`chat_stream`はfinish_reasonを`choice["delta"]`読取
の後に記録——deltaキー不在のfinish chunk(仕様合法の省略形)で
KeyError→continueが切捨て信号を消失させ、max_tokens切断回答を完了
として提示する経路を、捕捉を`isinstance(choice, dict)`ガード下で
前置へ移動して閉塞(v0.2.565)。全区間 fail-then-pass 規律で実証済み。

**v0.2.555-558 の要約**: 「evidenceなしをweak evidenceと区別する
検索設計対称 + 入力契約の全経路対称」区間——実欠陥3件・台帳同期1件
(v0.2.555は前区間の本台帳同期そのもの)。検索脚の死域閉塞:
`heapq.nlargest`は与えられた行から必ずk件を返すため、ベクトル脚が
全く信号を持たない場合(退化クエリベクトル・SHOIN_EMBED_MODEL切替後の
次元不一致・コーパス全直交)も行順で任意のkチャンクをvec=0.0で返却し、
RRF融合がそれらを実順位へ昇格させていた——次元不一致チャンクが最終
結果へ実測到達。`vec > 0`のみが順位枠を取得するよう修復し、空の
ベクトル脚は文書化されたBM25-only退行経路そのものとなる(v0.2.556)。
書込語彙の対称完結: `add_source`はタイトルをMAX_TITLE_LENに切詰める
がstripも空拒否もしなかった——`update_source_title`/`update_source_sha256`
が既に適用する検証を取込経路にも適用し、空白タイトルの永続化(空の
ソース一覧行・空TI・パンくず劣化)を閉塞。アップロード側は`.strip()`
追加で空白X-Filenameを`upload.txt`へフォールバック(v0.2.557)。
評価入力の製品契約対称: `parse_cases`はケース形状を検証するが
`MAX_QUESTION_LEN`超の質問(/ask・cli ask両経路が拒否するもの)を
無警告受理し、「製品が答えられない質問のrecall計測」＋数千語項の
病的FTS5 OR式を生成していた。重複質問も集計recall/MRRで二重計上
するため、同関数のrefuse-loudly契約に従い両者を`ValueError`拒否へ
(v0.2.558)。全区間 fail-then-pass 規律で実証済み。

**v0.2.531-554 の要約**: 「綴り揺れアークの完結——検索・再採点・
引用検証・冗長・切断・集計・重複排除まで全比較面の正準形統一」
区間——実欠陥21件・構造ピン2件・台帳同期1件(v0.2.531は前区間
v0.2.518-530の本台帳同期そのもの)。v0.2.526-530のUnicode可視化
アークが「クエリ用語が文書へ到達する」ことを保証したのに対し、
その先の全比較面がまだ生綴り(casefold/NFKCのみ)を照合していた
非対称を系統的に閉塞した区間。前段の境界残面を完走:
`_is_cjk_word`の残端(〠〶〷記号の可視化+Ogham空白の語分断)
(v0.2.532)→文分割へ非ASCII終止符11文字種(danda・ミャンマー・
クメール・チベット・アラビア・エチオピア・モンゴル・アルメニア・
ヘブライ)——1段落が1文扱いで文反復系検査全てが機能不全だった
構造欠陥(v0.2.533)→decimal行の双方向架橋(٣٤٥↔345、
unicodedata.decimal経由の閉置換)(v0.2.534)→アクセント記号の
列挙可能半分(`_ascii_fold`、café→cafe方向のみ——逆方向は開空間
のため構造的に不可)(v0.2.535)→英語活用の閉suffix族
(`_stem_variants`、-s/-ing/-ed/-ly——日本語側の漢字骨格と対称)
(v0.2.536)→否定フィルタの変種貫通(`-documents`が'document'も
落とす、取得-排除の対称契約)(v0.2.537)→否定語のみクエリの
補集合応答('-dogs'が[]→全件マイナスdogs、正規項ゼロ時の
静黙空返却を修復)(v0.2.538)。以後が本区間の核——比較面の
生綴り照合を逐次正準形へ: rerank/pool-IDF/近接の語彙重なりを
変種グループ照合へ(v0.2.539)→`_match_fold`新設で引用検証の
全比較をカタカナ→平仮名・Cf除去・数字行・旧字体・Latin特殊字の
正準形へ(v0.2.540)→`_digit_fold`で数値検査の同値数字行統一+
元号アラビア数字展開(v0.2.541)→拡張由来ヒットのcliff保護
(PRF/リライト語のみのヒットがterm-free切断されていた、拡張機構の
成果物を切断機構が剥がす非対称)(v0.2.542)→MMR冗長判定のfold化
(綴りのみ異なる実質重複チャンクを捕捉)(v0.2.543)→PRF集計キーの
fold化(綴り票分散で話題語が閾値未達)(v0.2.544)→リライト/提案
dedupのfold化(変種重複のスロット消費を解消)(v0.2.545)→
degenerate_spansのfold化(交互綴りパロットループを捕捉)
(v0.2.546)→英語`cannot`+curlyアポストロフィ縮約の否定検出
(否定パリティ盲化を修復)(v0.2.547)→修辞的問いかけ→回答ペアの
自己矛盾誤検知免除(`_claim_sents`へuncited側と同根拠の質問免除
配線——faq/study_guide型出力の構造的偽陽性を解消)(v0.2.548)。
メタガード2件(並行セッション作): テキストI/O全サイトの`encoding=`
明示化AST固定(v0.2.549)・env読取のconfig集中化+プロセスグローバル
変異動詞の全面封印(v0.2.550)。契約対称3件+UI1件: eval期待ソース
IDの不在警告(「静かに再鍵されたID」のゴーストケース可視化——
diff側ガードと対称)(v0.2.551)→overview文脈の等分配化(順位無し
呼出しでの調和級数が挿入順6倍傾斜を生んでいた)(v0.2.552)→
`note list`/`messages list`のNOTEBOOK_NOT_FOUND統一(読取系
2経路の「空と不在の混同」閉塞)(v0.2.553)→行keydownハンドラの
`e.target`ガード(バブルkeydownで子ボタンのネイティブ活性化を
preventDefaultがキャンセルし、リネーム中EnterがshowSourceを
二重発火していたキーボード操作性実害)(v0.2.554)。全区間
fail-then-pass規律で実証済み。残る構造的制約: ASCII→アクセント
文書方向・SHY位置方向は列挙不能の開空間として意図的に閉鎖。

**v0.2.518-530 の要約**: 「Unicode不可視アークの完結——query_termsの
静黙脱落クラスを4段階で根絶」区間——実欠陥4件(記号/文字種の不可視
+LIKE経路の否定フィルタ順序逆転+未定義CSS変数参照)・契約ピン6件・
台帳同期1件。v0.2.518はこの台帳自体の同期。まずメタ監査の残面を
完走: exceptハンドラ唯一の同型盲点`contextlib.suppress`を署名
カタログ化(v0.2.519)・`args.<attr>`読取り⊆宣言argparse destの
静的保証——dispatchテストが全フラグ経路を触れないため実行時のみ
発見される欠陥クラスを閉塞(v0.2.520)・補間正規表現の棚卸し——
`re.*`パターン引数への実行時項注入経路を在庫化し唯一の非エスケープ
経路を`re.escape`個別固定(v0.2.521)・Unicode-wide判定述語
(`isdigit`/`isalnum`族)の呼出サイト棚卸し——`'１２３４'.isdigit()`等の
想定外受理を`isascii`ガード/下流NFKCで正当化した全サイトを固定
(v0.2.522)・`querySelector`リテラルの全11種カタログ化——class/
属性リネームで選択器を更新し忘れると機能静死(修正シグナル皆無)する
欠陥クラスをドリフト検出で閉塞(v0.2.523)。後半は本区間の核:
CSS変数参照⊆定義ピンで`.src-rename`の`--ink`未定義参照(initialへの
シグナルなき退化=リネーム入力欄が既定色表示)を実欠陥修復
(v0.2.524)、`bm25_search`のLIKE-only経路が`[:k]`→negフィルタの
逆順で上位全否定時に結果が枯渇していた実欠陥を対称化(v0.2.525)、
そして`query_terms`不可視アーク4段: 囲みCJK/互換ブロック(㍻㋿㈱①㎏
等の和文頻出字が`_CJK_RANGES`未収録で用語脱落→追加、term_variants
の既存NFKC形が正準綴りへ橋渡し)(v0.2.527)→残り全NFKC折畳み
ブロック(Hangul Jamo・ローマ数字・上付き/分数・Letterlike・リガチャ・
変体仮名・数学英字・全角通貨)+NFD逆方向架橋(合成済クエリ→分解
Jamo文書=macOSファイル名由来)(v0.2.528)→非折畳み文字種
(Latin-1アクセント・キリル・ギリシャ・ヘブライ・アラビア・インド系等
40+ブロック——キリル/アラビアクエリは用語全落ちで**空返却**だった)。
`_is_cjk_word`へ`isalnum()||mark`カテゴリ経路を追加し結合記号
(matra/ニクダー/NFD音訓)で語継続・ブロック内句読点はカテゴリ検査で
自動境界化。ASCII↔非ASCII境界は維持で`'Python入門'`混合クエリの
narrowing退化なし(v0.2.529)→最終クラス=So/Sc/Sk記号・絵文字・
結合子(ZWNJ/ZWJ・VS1-16)を収録——絵文字単独クエリの空返却と
'👨‍💻'/'☕️'のシーケンス分断を解消し、`assertFalse(is_cjk("😀"))`の
旧契約ピンを反転(PUAを新しい非CJKプローブに)(v0.2.530)。
コスト0乗り逃げ文字が全滅したことで`_NON_CJK_RE`は非ASCII=
コンテンツの概算として機能。

**v0.2.496-517 の要約**: 「ピン系そのものの網羅性監査——メタ監査による
迂回経路の逐次封印」区間——構造ピン16件・実欠陥1件・債務完済・
文書同期2件。v0.2.496がこの台帳自体の同期。以後は各恒久ガードの
「検出器自身の盲点」を系統的に枯渇させるメタサイクル: ゲート免除
コメント(`noqa`/`type: ignore`/`pragma: no cover`+config経由の
ファイル単位縮小)の全在庫固定(v0.2.497)・ファイル変異動詞の
書込み面カタログ化(v0.2.498)・`except`ハンドラの特定型棚卸しと
silent-fallback本体の件数固定(v0.2.499)・`raise`送出面の対になる
カタログ化(v0.2.500)・DB chmod修復ループのシンボリックリンク
追従による権限改竄経路を閉塞+デコレータ許可集合化(v0.2.501)・
時計/並行呼出面の棚卸し+監視モジュールのエイリアス迂回禁止
(v0.2.511)・from-import裸名束縛+動的dispatch+属性再束縛+
スターimportの封印(v0.2.512)・危険動詞の「値化」経路
(partial/map/ハンドラ引数)+`sys.modules`/`globals()`/`builtins.`
綴り+`operator.methodcaller`文字列密輸の封印——アノテーション
位置を豁免(v0.2.513)・能力import=付与面の棚卸し固定
(v0.2.514)・dunder横断(`__globals__`/`__subclasses__`等)全面
禁止+モジュールローダ/代替プロトコルへcapability拡張
(v0.2.515)・監視モジュール名前空間への書込み=ランタイム
モンキーパッチ面の禁止+`setattr`/`delattr`をdynamic集合へ
(v0.2.516)・`global`/`nonlocal`/`del`/`TYPE_CHECKING`ブロックの
ステートメント面ゼロ在庫化(v0.2.517)。

**v0.2.472-495 の要約**: 「失敗の可視化——サイレント拒絶系の閉塞と
E501債務の完済」区間——UI実欠陥3件・情報欠陥1件・構造ピン8件・
債務返済5件・文書同期3件。境界失敗の不可視クラスを連続閉塞:
`loadNotebooks`がfetch+`.json()`をtry外に置く唯一のasync関数で、
エラーがunhandledrejectionとしてコンソールのみに漏れていた経路
(v0.2.483)。`localStorage`裸アクセスがスクリプトトップレベル評価で
SecurityErrorを起こし、マークアップは描画されるが全コントロールが
無反応になる最悪の失敗形態(プライベートモード制限・sandbox環境)
を`_lsGet`/`_lsSet`集約で解消(v0.2.484)。コレクション読取りの防御
姿勢が同一関数内で非一貫(`cur.sources.forEach`裸呼びと`?.length`
ガードの混在)だったのを`cur = {...defaults, ...j}`境界正規化へ
転換(v0.2.487)。lazy `<details>`全文ロードが`dataset.loaded`を
fetch成功前にセットしcatchでクリアしなかったため、一時的失敗の
エラー文がビューアセッション中永久に残るリトライ不能経路
(v0.2.489)。SSEエラーフレームは例外型名のみを漏洩へ変更
(v0.2.476)。構造ピン群: ルートパターンの`^..$`完全アンカー化
(v0.2.477)・ルートアリティ(数値群≡ハンドラ引数)固定(v0.2.478)・
ruffゲートを全クリーン規則ファミリへ拡大(v0.2.479)・exceptの
tuple/`except*`バイパス閉塞(v0.2.480)・env読取りのconfig集中化と
プロセスグローバル変異動詞封印(v0.2.481)・テキストI/Oの全
`encoding=`明示化(v0.2.482)・タイムスタンプ単一プロデューサー
契約(v0.2.485)・危険プリミティブ拡張——breakpoint/exit/pdb/
warnings/traceback(v0.2.486)・削除/非推奨stdlibインポート全面
禁止+`sre_parse`→`re._parser`移行(v0.2.488)。E501長行債務は
220件祖父済みを縮小のみ許可のラチェットで管理開始(v0.2.490)、
5回の返済(8→48→25→25→114サイト)で220→0完済——ベースライン
空化によりツリー内全ファイルがbudget 0の永久抑止下へ
(v0.2.491-495)。v0.2.472/473/475は台帳・仕様書の同期。

**v0.2.463-466 の要約**: 「送出コードの命名契約——32コード集合の
カタログ封印」区間——構造ピン1件・文書3件。`_dispatch`はエラー
コードを接尾辞/接頭辞写像(`*_NOT_FOUND`→404・`*_ALREADY_EXISTS`
→409・`SYSTEM_*`→500・他→400)するため、規約外の綴りは静かに
400バケットへ落下する——raise/emit全コードを32件宣言集合＋名族
タクソノミーでAST固定し、新コードには文書化根拠のカタログ更新を
必須化(v0.2.465)。同サイクルはemit応答メタ・SSRFリダイレクト
各ホップ再検証・questions_cache無効化・busy_timeout・env-var
双方向一致・ORDER BY決定性を再確認して全クリーン。v0.2.463/464/
466は台帳・仕様書の同期。

**v0.2.436-440 の要約**: 「境界不変条件の実証——サイズガードの
読み取り順序と圧縮展開」区間——実欠陥1件・構造ピン2件・文書同期2件。
`extract_file`が`read_bytes()`で全ファイルをメモリに載せてから
`_check_size`を呼んでいた——巨大ローカルファイルでも上限検査が
発火する前に全量バッファリングされ、ガードが最も必要な場面で
無効化されていた順序欠陥を`stat().st_size`事前拒否で修正
(v0.2.437)。`fetch_url`の10MB上限は圧縮後バイトのみを縛るが、
`_decode_content_encoding`内部が展開後サイズを既に検証——
変異検証で防御位置を突き止めた上で、唯一テスト不在だった
この不変条件をend-to-endピン化(gzip bomb→INGEST_FILE_TOO_LARGE)
(v0.2.438)。`generation_lock`配下のLLM生成呼出し3サイトを
カバレッジスキャンで封印——ロック無しの新呼出しサイトは
正しく動作するため全テストを素通りする構造ホール(v0.2.439)。
v0.2.436/440は台帳・仕様書の同期。

**v0.2.431-435 の要約**: 「並行性契約の恒久封印——TXクラスの第4壁と
共有キャッシュのlock-coverage」区間——構造ピン3件・文書同期2件・
文書訂正1件。`Store.__enter__`/`__exit__`は今日はclose()のみだが、
将来のcommit追加を止めるものがなかった——withブロック途中失敗の
pending書込みがクリーンアップで公開される経路をASTピンで閉塞し、
v0.2.419-425で文・呼出し・入れ子の三面を封じたペンディングTX
欠陥クラスを完全閉鎖(v0.2.432)。ハンドラスレッド共有のモジュール
レベルキャッシュ2面——`questions_cache`(dict check-then-set、
v0.2.395のstale-fingerprint上書きレースを塞いだロック)と
`_QUERY_VEC_CACHE`(OrderedDictのLRU変異)——は無防備アクセスが
単一スレッドでは動作するため全テストを素通りする構造ホールだった。
両方の全アクセスを`with X_lock:`内必須化するスキャナで封印
(v0.2.433/434)。副産物: CLAUDE.mdの融合箇条が削除済みの
fuse()/adaptive_alpha()「残存」を主張していた陳腐化を訂正(v0.2.432)。
v0.2.431/435は台帳・仕様書の同期。

**v0.2.426-430 の要約**: 「契約ピンの外延——kind語彙・リソース寿命・
入力文字クラス」区間——実欠陥1件・構造ピン2件・文書同期2件。
`_INSTRUCTIONS`のキー集合がSTUDIO_KINDSと非対称のまま語彙追加されると、
ハンドラ検証を通ったkindが`_t_kind`でKeyError→StoreError系のcoded-error
写像を抜けて生500化する経路をキー集合同値ピンで閉塞(v0.2.427)。
`Store(...)`呼出し全てをASTレベルで`with`のcontext式必須化——裸生成は
thread-affinedなsqlite3接続をclose不能のままリークし、per-request
パターンでは無制限のfdリークになる経路を封印(v0.2.428)。
`_read_json`の`\ud800`型エスケープが単独サロゲートとして受理され、
書込み時にsqlite3バインドで未捕捉UnicodeEncodeError→生500化する
経路を、`_check_utf8`(UTF-8往復検証)を全バリデータへ導入して
400 `VALIDATION_FIELD_FORMAT_INVALID`化(v0.2.430)——入力境界の
最後の未防御文字クラス。v0.2.429は仕様書同期(TX契約3層ピン・
語彙ピン・Store-with式ピンをDB節へ折込)、v0.2.426はこの台帳自体の同期。

**v0.2.420-425 の要約**: 「ペンディングTX欠陥クラスの構造封印——挙動修正
から恒久的な契約ピンへ」区間——実欠陥1件・構造ピン3件・文書同期2件。
`chat_stream`のdelta解析がdict形状を仮定し、互換サーバの裸文字列deltaや
role-onlyチャンクのnull deltaで`.get`がAttributeError→生500に化ける
(v0.2.259正規化哲学の残件)を形状正規化で閉塞(v0.2.421)。v0.2.419は
欠陥クラスを7サイトの挙動修正で潰したが、将来のwriterが裸commit列へ
回帰する余地は残った——3連ピンで構造封印: 書込み動詞executeの
`with self.conn:`内必須スキャン(単文writer・callee契約はcap付きallowlist、
fail検証で複数行シグネチャ閉じ行`) -> T:`がメソッド追跡を早期終了させる
ピン自身の盲点を発見・修正)(v0.2.423)、callee-transactedヘルパ3種の
全16呼出しサイトのwith内必須化+`_set_embedding_pair`の`set_embedding`
単一caller化(v0.2.424)、with所有メソッド12個の入れ子呼出しゼロ
(sqlite3の`with`は__exit__でcommit=外側pending早期確定の危険経路)
(v0.2.425)。store.py外の`.conn.`参照も全棚卸し——read SELECTまたは
`_embed_chunks`のTX所有commit/rollbackのみでTX面は完全閉鎖。
v0.2.420/422は台帳・仕様書同期。

**v0.2.414-419 の要約**: 「死データ蓄積の発見 → ペンディングTX欠陥クラスの
全局面根絶」区間——実欠陥1件・レビュー駆動修正2件・クラス一掃1件・
文書同期2件。`studio_outputs`は再生成のたび追記されるが唯一の読取経路
(latest_studio_outputsのMAX(id)群)に旧行は永遠に届かず、無制限の死データと
して蓄積していた——`add_studio_output`が同TX内でkind一致の旧行をprune
(v0.2.416)。Devin Reviewが同修正の残余を2連で指摘: DELETE-firstはINSERT
失敗時に消去がペンディング残りし後続commitが前行を消す(v0.2.417で
INSERT-then-DELETE + `id < lastrowid`化)、prune失敗は逆にrejected行を
publishする(v0.2.418で`with self.conn:`原子化)。その教訓を全Store書込みへ
展開——bare書込み+遅いcommitで第二文(touch/embedding_norm)失敗時に先行
書込みが宙に浮く同型が`add_source`/`delete_source`/`add_note`/`delete_note`/
`add_message`/`clear_messages`/`set_embedding`の7サイトに残存していたため
全て`with self.conn:`へ統一し、`_RacyConn`ロールバックテスト7件で全方向
ピン(v0.2.419)。README機能一覧に出荷済みノート/エクスポートの記載が
欠落していた「省略による半真」を解消(v0.2.415)。v0.2.414はこの台帳自体の
同期。

**v0.2.374-380 の要約**: 「書込み面の兄弟メソッド対称性 + テスト自身の検証品質」
区間——契約ピンが全層を網羅した後の残存欠陥は「同じ責務を持つ2メソッドの非対称」
と「ピン自身の自己カバレッジ欠落」へ移行。実欠陥修正3件: UIファイルピッカーの
`accept=`が`ingest._EXT_KIND`の6拡張子に対し4件しか載せておらず`.markdown`/`.htm`
が選択不能だった(v0.2.375)、`add_chunks`が全書込みopで唯一`touch_notebook`を
呼んでいなかった潜伏欠陥(v0.2.376)、`update_source_sha256`がtitle更新に
`_rewrite_chunk_context_titles`もstrip/emptyガードも持たなかった兄弟非対称
——改名経路で2度修正済みのFTS陳腐化クラスが休眠APIに残っていた(v0.2.377)。
ピン完全化: touch契約列挙が全12書込みopを網羅(v0.2.376で`rename`/`clear`/`add_chunks`
追加、v0.2.378で`replace_chunks_for_source`追加・機械的棚卸しで6個の正当除外を
明記)。テスト品質面: `assertRaises(StoreError)`の`.exception.code`未検証3箇所を
修正——うち1件はrefresh衝突テストがNOTEBOOK_NOT_FOUNDでもグリーンになる実弱点
(v0.2.379)、スキャナをLLMError/IngestErrorへ一般化し`with (..., assertRaises, ...)`
タプル形式(副作用検証型)のみ免除(v0.2.380)。実修正3件・ピン/規約強化4件で、
「ピンが存在するが守備範囲が自身を含まない」欠陥クラスの最終層を閉塞。

**v0.2.339-373 の要約**: 「契約面の全層走査完走 + 値レベル意味論への深化」区間——
契約ピン手法がHTTP面・i18n面・文書面・DB面・スキーマ面を端から端まで走査し、
残存していた欠陥は「定義が2箇所ある箇所の静かな乖離」ではなく「単一定義の
意味論そのもの」へ移行した。i18n面の最終層: 呼出し側の`.replace`/`format`
キーワード代入の完全性(v0.2.340-341、エイリアスimport `_qa_t` まで辿る
v0.2.342)。文書面: CLAUDE.mdのサブコマンド一覧欠落(messages/eval、v0.2.343)、
文書参照env変数≡コード読取名(v0.2.344)。HTTP面は双方向を値まで固定: fetch
動詞≡ルート表(v0.2.345)、リテラルid参照≡実要素(v0.2.346→347でaria/for/href
アンカーへ拡張)、data-i18n*属性種別カバレッジ(v0.2.348)、`_t`リテラルキー⊆
_STRINGS(v0.2.349)、SSEペイロード双方向(v0.2.350)、リクエストボディ≡ハンドラ
読取り(v0.2.351)、応答読取り≡発行キー(v0.2.352)、ルート表整合性+メタ名
(v0.2.353)、テンプレート/値契約(v0.2.354)、マークアップ健全性+オフライン範囲
(v0.2.355)、SQL補間安全性+エラーコード体系(v0.2.356)。UI面: 五点バンプ儀式の
マーカー同値(v0.2.357)、dead CSS2件修正+クラス名双方向(v0.2.358)、a11y字句契約
(v0.2.359)、**フォームコントロール命名の実欠陥修正**——主入力4件がプレース
ホルダのみ名無しだった(v0.2.360)、**見出しレベル飛びの実欠陥修正**(viewerのh3、
v0.2.361)、スクリプト衛生+フォーカス可視性(v0.2.362)。DB/スキーマ面: マイグレーション
版数の追記専用昇順(v0.2.363)、接続PRAGMAの実接続検証(v0.2.364)、FTS更新トリガの
改名同期(v0.2.365)、updated_at全書込みタッチ(v0.2.366)、チャンク投影getterの
カラム形状(v0.2.367)、**messages.role語彙の書込み時検証追加**——typo'd roleが
履歴交替を静かに破壊する経路(v0.2.368)、embed_model設定キーの単一ソース化
(v0.2.369)、counts集計2経路一致(v0.2.370)、**SYSTEM_* StoreErrorの400→500
誤写像修正**(サーバ障害がクライアントエラーに化けていた、v0.2.371)、recent-
messages ordering+ソースgetter parity(v0.2.372)、studio出力のkind毎最新意味論
(v0.2.373)。実修正5件(v0.2.358/360/361/368/371)・実リファクタ1件(v0.2.369)を
含み、残りは監査クリーン面の恒久ピン化。

**v0.2.332-338 の要約**: 「i18n契約の完全閉塞 + パッケージ契約の実機検証」区間——
滞留していたレビュー対応コミット(PR#122先端の未マージ分)を回収: eval差分の
重複質問をoccurrence単位でペアリングし幻影deltaを解消、比較行をmatched-population
平均へ(v0.2.334)。READMEに`eval --save/--diff`のA/B比較導線を記載——推奨フローの
手段が`--help`以外に存在しなかった欠落を解消(v0.2.335)。i18n面はキー対称性を超えて
**値の中身**までピン: サーバ5テーブルの`{name}`プレースホルダ集合同値(v0.2.336、
`_t`はformat経由で片ロケールのみKeyError)とUI側I18N値のプレースホルダ同値
(v0.2.337、呼出し側が手動`.replace`代入のため片ロケールへ生`{n}`漏出)。
パッケージング面はAST走査で非stdlibインポート集合≡pyproject dependenciesを
双方向固定(v0.2.338、lazy importのpypdfも捕捉)し、ビルドしたwheelをclean venvへ
インストール→`shoin`→new/add/askのゴールデンパスを実機検証(3.9環境では
requires-pythonが正しく拒否)。監査クリーン確認: evalベースラインI/Oの全ガード
(report_from_dict型検証・k不一致警告)、jpost/api()境界の双方向契約、
ルート浮遊ファイルゼロ、全skipTestは環境条件付きで正当。リポジトリ衛生: 陳腐PR
17件・リモートブランチ152本・ローカル154本を包含検証付きで整理——open PRは
ロールアップ#160のみに集約。

**v0.2.323-331 の要約**: 「契約層の残存全面ピン」区間——ソース行の最終3配線
(×削除のdisable→DELETE→reload/失敗復帰、rename中の行clickガード、
ondblclick→startSourceRename、v0.2.330)でindex.html全イベントハンドラの
挙動固定が完結。viewer側はabort guard・フォーカストラップ・遅延details
toggle・死んだソケットのdelta書込み尻尾を固定(v0.2.324/326——イベント条件付き
side_effectで順序依存カバレッジを決定論化)。JS↔Pythonの横断定数は残2件を
同値固定(COVERAGE_LOW閾値とapi()の{error:{code,message}}→`[code] msg`写像、
v0.2.325)、UIが読む`report.*`キー⊆CitationReport(v0.2.328)とリテラル`t()`
キー⊆I18N.ja+ja≡en対称(v0.2.329)で生産者↔消費者契約を封塞。公開仕様は
spec.mdを実装へ同期(STRIDE行に出荷済防御を記載、v0.2.327)。エージェント文書の
完了条件が`unittest`のみで残4ゲートを黙殺していたドリフトを修正——verify.sh
全ゲート+`mypy --strict`+`PYTHON=`ノブへ(v0.2.331、v0.2.301と同欠陥クラス)。
監査クリーン確認: refresh_sourceの非URL拒否(INGEST_REFRESH_NOT_URL)・sha衝突
事前検査・byte同一no-op、updated_atの全書込み経路一貫(embeddings/settings/
migrateは正しく除外)、questions_cache失効(refresh/rename両経路)、
add_source重複(UNIQUE+事前チェック+競合マップ)。

**v0.2.307-322 の要約**: 「到達可能ガードの全完走 + 横断列挙の同値固定」区間——
カバレッジ未検証27行を同一欠陥クラス(一度も発火していないエラーマッピング
ガード)と特定し全到達可能分を固定(v0.2.321)。`_RacyConn`プロキシ新設で、
SQLトリガ一致時に同時削除を注入しstore.pyのrowcount==0/FKガード
(update_source_title・update_source_sha256・replace_chunks両経路・
delete_note)がStoreErrorへ正しく写像されることを決定論的に検証——残3行は
構造的に到達不能と証明済み。サーバ面では受容ソケットにREQUEST_SOCKET_SEC=
120を設け不完全リクエストのスレッド永久占有を遮断しhandle_errorで
TimeoutErrorを黙殺(v0.2.315)、send_errorをJSONエンベロープ経路へ統一し
プロトコル層エラー(OPTIONS等)にもnosniff/no-store/Referrer-Policyを送出
(v0.2.316)、ServerヘッダからPythonランタイム版露出を除去(v0.2.313)、
深ネストJSONのRecursionErrorを500→400契約へ修正(v0.2.314)。UI面は
`role="tablist"`が宣言していた矢印キー/Home/End操作を実装しaria-controls
↔tabpanelで正式関連付け(v0.2.311)、以後全インタラクティブハンドラ
(作成・追加・削除・生成・言語切替・SSEフレームパーサ)をnode実ハンドラ駆動で
挙動固定——**全書込み経路のガード完備**(v0.2.317-320)。横断列挙も同値固定:
UIの`const KINDS`≡`studio.KINDS`、export href集合≡`export.FORMATS`
(v0.2.322)。UX実欠陥としてノートブック一覧のタイムスタンプ同値時非決定性を
`ORDER BY updated_at DESC, id DESC`で解消(v0.2.308)。運用面: gitignoreに
build/test/tool全アーティファクト追加(v0.2.309)、wheel収録スコープを
`shoin/**`のみと実測+恒久ガード化(v0.2.310)。nosniff+Referrer-Policyは
全応答クラスへスイープ拡張済み(v0.2.312)。

**v0.2.297-306 の要約**: 「並行構造のサイレントドリフト」欠陥クラスの掃討区間——
同じことを二度定義している箇所は全て、片方だけが静かに腐りうる。ゲート定義
(ci.yml↔verify.sh↔pre-push、v0.2.298)、テスト探索パターンとトップレベル
パッケージ範囲(v0.2.299)、.gitignoreのSQLiteサイドカー/-env.*カバレッジ
(v0.2.300)、貢献者手順書のpytest誤記(v0.2.301)、エージェント作業指示書の
bump儀式ラベル誤記(三点→五点、v0.2.302)、追跡され続けた陳腐SBOM凍結
スナップショットの解除(v0.2.303)、READMEの```json例と実スキーマの結合
(v0.2.304)、CLAUDE.mdのbudget定数誤記——「6 messages, 160 each」(=960)は
実コードのHISTORY_TOKENS_TOTAL=400と矛盾し、「split equally」はv0.2.200の
harmonic化以降4版遅れ(v0.2.305)、そしてゲートツール自体の浮動バージョンを
requirements-dev.txtにピンしdependabot週次bump経路へ接続(v0.2.306)——の
全てを恒久ガード化した。コード面は全モジュール監査クリーンのため、価値の
重心は不変条件ピンへ移行。リリース経路も実証済み: pip install→shoin serve
でUI全文+/api/health応答、pypdf>=4.0下限は実際の4.0.0で動作確認。

**v0.2.291-296 の要約**: 「守りの宣言的側面」——入力検証・台帳・パーミッションの
欠陥クラスを端から端まで掃討した区間。CLI数値フラグの無検証経路をargparse層で拒否化
(`-k`≥1・`--port`0-65535、`k<0`の`merged[:-n]`スライスや範囲外ポートの生tracebackを
usage errorへ、v0.2.292)し、同型をenv面にも展開(`SHOIN_PORT`範囲外→デフォルト
フォールバック、v0.2.293)。パッケージングではstatic資産全てがpackage-data globに
含まれることを恒久ガード化(v0.2.294、wheel実測でindex.html同梱確認済み)。台帳では
**38版分のHISTORY.mdエントリ欠落**を発見・修復——追記アンカーが存在しない見出しを
指してサイレントno-op化していた儀式破損で、正直な統合バックフィル+`### v{VERSION}`
存在ガードで再発を遮断(v0.2.295)。プライバシー面ではDBのumask由来644/データdir755を
プリ作成0600+自前data_dir()のみ0700修復へ——`--db`の外部dirは不触(v0.2.296)。
ネットワーク・パイプライン・ハンドラ面は監査クリーン確認(loopback guard・Host/Origin
検証・CSP・bounded読込みは全て既ガード済み)。

**v0.2.283-290 の要約**: 監査網が構造的欠陥と性能面へ拡張された区間。
スレッド安全監査で実レース1件——SSE経路の`last_finish_reason`が`generation_lock`
解放後に読まれ並行要求で打切り信号を喪失/誤帰属し得た(v0.2.284、ロック内捕捉へ)。
HTTP面では`Cache-Control: no-store`がSSEのみ送信だった構造的欠陥を`_headers`
チョークポイントへの移設で全応答へ拡張(v0.2.285——サーバー更新後に古いindex.htmlが
キャッシュで生き残るUI/API skewを閉塞)。CLI↔API契約の非対称も解消: `_cmd_ask`が
空白のみ質問を拒否しなかった経路を`VALIDATION_REQUIRED_FIELD_MISSING`で統一
(v0.2.286)。仕様面ではREQ網羅性を恒久ガード化——spec.mdの全REQ-*がコード/テストに
参照されることを固定(v0.2.287、REQ-001/007/105/106は未参照だった)。ツールチェーンでは
verify.shの「same checks ci/ci.yml runs」が誤記だったのを修正——detect-secretsゲートを
移植し秘密漏洩をコミット時遮断(v0.2.288)。clean-by-construction領域もガード化:
index.htmlのinnerHTML/eval系シンク非存在とexport FORMATS↔MIME/EXT表一致を恒久固定
(v0.2.289)。性能面では`_hard_split`/マージループの`estimate_tokens(buf+next)`再走査を
走行カウンタへ換え改行稠密入力を84倍高速化(v0.2.290、10万行で7.5秒→0.09秒)。
エクスポート・fetch_urlリダイレクト・SSE/ストリーム・ORDER BY決定性は監査クリーン確認。

**v0.2.276-282 の要約**: カバレッジテールが実質完走し、主題は「監査が欠陥ゼロを
出した領域の不変条件をテストとして恒久固定」へ移った区間。残存49miss行を17テストで
固定し(v0.2.277——latin-1ファイル名復元・ゾーン付きアドレス拒否・破損deflate・
5種不一致ビット・非整数env・qa.py 4経路・vector経路negフィルタ・令和元バリアント・
長ラン周期クレジット・refresh sha衝突・truncated伝搬)、残19行は全てソケット切断/
並行削除/構造上到達不能のみ。続いて仕様面を実装へ同期: spec.md乖離6箇所(v0.2.278)、
README訂正2箇所(v0.2.279)、性能主張の計測エンベロープ記注(4.1MB/2000チャンクで
検索~120ms ≤200ms目標内、回答p95は実モデル依存で未検証と明記、v0.2.282)。
監査済みクリーン領域は不変条件化して封じた: サーバー側i18n ja/en対称
(6テーブル、UI側と並走、v0.2.280)と `python -O` 耐性(store.pyのassertを
明示raise化+ASTガードで全assert禁止、v0.2.281)。

**v0.2.269-275 の要約**: 欠陥空間枯渇後、主題は「防御テールまでの検証網の
敷き詰め」に移行しカバレッジが95%→**99%**に達した区間。`cli.py` のエラーパス
(eval系のファイル読込/JSON形状/ベースライン同一3連・serve OSError→
SYSTEM_PORT_IN_USE・KeyboardInterrupt→rc130、v0.2.270)を起動して固定した後、
`_h_ask_sse` の build_context 例外経路(ヘッダコミット後はSSE errorフレーム+
空assistantメッセージ永続化でuserターンの孤立を防止、v0.2.271)を実HTTP越しに
検証。続いて純関数テールを一括固定: search.py 7件(万剰余変種・negのみクエリ・
cos次元不一致ガード・_minmax空・proximity左端縮退の厳密値・RRF信号マージ・
retrieve_multi空ガード、v0.2.272)と citation.py 12行中11行(同族単位連鎖
1km500m→1500・解析不能漢数字の沈黙4経路・引用のみ先頭文のskip・suggested命名・
source_detail長不一致raise、v0.2.273-274——前者の追加検証で区切り文字がbareを
残す仕様を確認し「[S1]単独」が唯一の発火条件と確定)。最後にcli.py印刷面を完走し
94%→99%へ(フラグマーカー3種・uncited supported命名・contradict/truncated・
eval失敗ケース詳細・diffのk警告/差分/増減尾部・serve成功rc0・pages_failed
警告がadd/refresh両経路で出ること、v0.2.275)。残miss71→49行は全て
ソケット切断依存(server.py ConnectionError尾部)または構造上到達不能な防御行で、
カバレッジテールは実質完走した。

**v0.2.263-268 の要約**: 欠陥空間が実質的に枯渇した区間——再監査は
`_check_embed_model_ok`・`_dispatch`・`overview_hits`・`loadNotebooks`・
I18N双言語対称性(86/86一致)を含む全面で欠陥ゼロを確認し、主題は「動作未固定の
UI機構をnode実行で構造的に封じる」へ移った。まず `health()` の fetch 失敗経路が
`llm:false` と異なる表示(lamp・banner未更新)をしていた不誠実面を解消(v0.2.264——
この区間唯一の実欠陥)。続いて質問候補チップ `refreshQuestions`(ガード3条件・
ノートブック切替レース棄却・chip→入力充填)、`renderNotebook` の pendingRename
(rename入力の再レンダ跨ぎstash/restore——activeElement検出と
externalPendingRenameの2経路・幻PATCH防止・幽霊棄却)、`startSourceRename` の
5分岐(Enter commit・sibling-blur skip・Escape取消・二重commitガード・
空/未変更→reloadのみ)、`loadNotebooks`(curハイライト・削除後auto-open・
confirm/promptガード)を連続でnode実行固定(v0.2.265-268)。これにより
index.html の全多分岐経路——SSEリーダ・seal・studio・note・questions・
rename・サイドバー——が動作テスト下に入った。

**v0.2.253-262 の要約**(この台帳が v0.2.252 で止まっていたため追記): 主題は
「自己矛盾と無言劣化の境界」をさらに一段深く閉じることだった。境界値の整合は
`expand_query` を `MAX_QUESTION_LEN` で縛り(v0.2.254 — 検証と実行の境界乖離)、
PATCH 応答が実際に保存された name を返す(v0.2.255)。取込側は HTML 抽出から
nav/footer/form のボイラープレートを除外(v0.2.256)、PDF のページ抽出失敗を
`pages_failed` として API・CLI・UI 全面で表出(v0.2.257-258、refresh経路まで)。
応答形状は LLM の content 配列を str() 強制ではなく正規化(v0.2.259)、
`suggest_questions` が /ask が拒否する超過長・重複を提案し得た自己矛盾を解消
(v0.2.260 — アプリが自分の答えられない質問をチップとして出していた)。検索側は
ベクトル次元不一致を `_cosine_with_norms` で 0.0 へ(モデル変更忘れや破損BLOBが
断片dotで偽スコアを生成していた、v0.2.261)。CLI は `---` レポートガードの
欠落キー(degenerate/self_contradiction)を共有述語 `_report_has_output` で構造的に
閉塞——同ガードがキーを見落とすのは3度目であり再発を防ぐ(v0.2.262)。全区間
fail-then-pass 規律で実証済み。

**v0.2.233-252 の要約**(この台帳が v0.2.231 で止まっていたため追記): 欠陥空間は
表層から深部へ進み、この区間の主題は「同型欠陥の構造的根絶」と「無言劣化経路の
閉塞」だった。UI警告面は重複していたバッジ連鎖を共有 `reportBadges()` へ一本化し
(v0.2.235-236、SSE live・永続履歴・Studio で全フラグ parity、`coverage:null` の
誤発火経路も統一で解消)、シール操作は同名ソース衝突時に抜粋照合で正出典を開く
(v0.2.239)。検索側は LIKE フォールバックの「先着2000」をスコア順「上位2000」へ
(v0.2.237)、否定項の部分一致誤除外を `query_terms` と同じ語境界へ(v0.2.238)、
ストップワード needle の過剰支配を Lucene 標準リストで除去(v0.2.244)。誠実性は
打切り回答の `truncated` を全4面へ(v0.2.245)、LLM稼働中の検索のみ劣化を示す死んで
いたペイン badge を配線(v0.2.240)、SSE の error/done 欠落を表出(v0.2.241-242)。
生存性は SSE 切断時に永続済み回答を同経路で復元(v0.2.246)、`openNotebook` の逆順
応答を系列ガードで破棄(v0.2.249)。取込は Content-Encoding(gzip/deflate/br拒否)の
デコード(v0.2.248)、同一内容 refresh の真 no-op 化(v0.2.243)。運用境界は
`GET /api/notebooks/{id}` の無制限 messages を上限500+`messages_omitted` 開示
(v0.2.250)、質問埋め込みの有界 LRU 化(v0.2.251)、eval --diff の集計を共通質問
のみへ(v0.2.252 — ケース集の編集がスコア変動に混入していた計測の嘘)。キャッシュ
失効の抜けは reindex まで網羅(v0.2.247)。全区間 fail-then-pass 規律で実証済み。

**v0.2.180-231 の要約**(この台帳が v0.2.179 で止まっていたため追記): 引用検証
スイートは四段から十検査へ拡張され——数値一致(位取り漢数字・英語数詞・歩合・
率・元号の展開、単位ファミリ換算の誤検出抑制)、逐語引用(doctored quote検出
付き)、単位一致、否定反転(反義語・程度語含む)、自己矛盾(ターン横断)、退化
ループ(ターン横断)——「検出→分類→特定→修正ヒント」の鎖が全警告で完結
(misattributed_suggested/uncited_supported_source が正出典を名指し)。
出所の説明可能性は抜粋・節に加え検出経路(source_detail=全文/意味のどちらが
拾ったか)までUI/CLI/exportの全面に揃った。検索側は表記橋渡しが完結(幅・
カナ・数字・活用骨格・元号・旧字体)+コンテキストパンくずのBM25加重+
PRF(MQR)+近接ボーナス+語彙なしベクトル末尾のcliff切断。生成側は
順位比例トークン予算・隣接チャンクマージ・セグメント毎§ラベル・切詰め
マーク・max_tokens暴走抑止。構造認識はフェンス/インデントコード・箇条書き
・枠組み文の免除まで。評価は eval --save/--diff でA/B比較可能に。検出した
静かな誤帰属バグ: refresh後のrowid再利用が「引用箇所」マークを誤置(v0.2.230)
——同修正が初の行動的UIテスト(node実行renderFullSource)をもたらした。
公開仕様は3面+本台帳が実装と同期済み(v0.2.231-232)。

v0.2.158-169 では三つの台帳(長所・短所・改善案)そのものを反証にかけ、
性能を端から端まで実測した:

- **長所は反証テスト済み** — 「完全ローカル」を outbound 通信の全列挙で裏付け、
  スキーマのダウングレード安全性も検証(v0.2.158)。
- **短所側の最後のエンジニアリング欠陥を解消** — 改名後の埋め込み陳腐化(v0.2.160)。
- **台帳自体の陳腐化を検出** — 完了済み項目が開いたまま残り、習慣が成果物として
  並んでいた(v0.2.161)。
- **性能を初めて実測** — 検索3.96倍・メモリ O(k)・取込23倍。文書化上限での
  実使用可能性を数値で確認し、利用者向けの目安を README に掲載(v0.2.162-166)。
- **自分の修正を二度、敵対的に読み直して欠陥を発見** — ノルムキャッシュが
  ダウングレード安全性を壊し(v0.2.167)、その修正が reindex のキャッシュを
  捨てていた(v0.2.168)。修正もまた変更であり、同じ疑いを向けるべきである。
- **11ラウンドが噛み合うことを実機で確認**(clean venv, schema 9, 全経路, v0.2.169)。

残る弱点はコード品質ではなく**配送面と意図的トレードオフのみ**であり、
バックログに開いたエンジニアリング項目は無い。
