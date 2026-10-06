"use strict";
/* ---------- i18n (namespace.component.key) ---------- */
const I18N = {
  ja: {
    "app.title":"Shoin 書院","app.lamp.on":"LLM接続中","app.lamp.off":"LLM未接続",
    "tabs.sources":"資料","tabs.chat":"対話","tabs.studio":"書斎",
    "sources.head":"書 棚","sources.empty.title":"書棚が空",
    "sources.empty.body":"ファイルかURLを加えると、引用付きで対話できる。",
    "sources.addurl":"取込","sources.added":"取込完了","src.pages_failed":"⚠ {n} ページの抽出に失敗（索引が不完全です）","src.embed_short":"⚠ 埋め込み {n}/{total} 件 — 意味検索が不完全です",
    "nb.placeholder":"新しい書院の名前","nb.create":"作成","nb.rename":"名前を変更","nb.delete":"この書院を削除",
    "nb.empty.title":"まだ書院がありません","nb.empty.body":"名前を付けて最初の書院を作る。",
    "chat.head":"文 机","chat.placeholder":"資料への質問…","chat.ask":"尋ねる","chat.clear":"クリア",
    "chat.empty.title":"問いから始まる","chat.empty.body":"下の入力欄から、資料に基づく質問をどうぞ。",
    "chat.you":"あなた","chat.shoin":"書院","chat.degraded":"検索のみ",
    "chat.coverage.low":"引用被覆 低","chat.invalid":"検証失敗の引用",
    "chat.misattr":"誤ソースの可能性","chat.numeric":"数値が出典に無し","chat.unit":"単位が出典と不一致","chat.negation":"出典と逆の主張の可能性","chat.confirmed":"根拠確認済み",
    "chat.uncited":"無出典の断定文","chat.uncited_supported":"出典内一致=引用欠落","chat.degenerate":"繰り返し生成の疑い","chat.contradict":"前後の記述が矛盾",
    "chat.truncated":"出力が途中で打ち切られた可能性","chat.truncated_hint":"トークン上限に達して生成が停止した応答です",
    "chat.stream_dropped":"応答ストリームが途中で切れました",
    "chat.earlier":"— 以前の {n} 件は省略 —",
    "studio.head":"書 斎","studio.briefing":"ブリーフィング","studio.study_guide":"学習ガイド",
    "studio.faq":"FAQ","studio.timeline":"年表","studio.mindmap":"マインドマップ",
    "studio.empty.title":"まだ出力がありません","studio.empty.body":"上のボタンで書院に生成させる。",
    "studio.savenote":"ノートに保存","studio.savenote.ok":"ノートに保存しました",
    "notes.head":"ノ ー ト","notes.title":"題","notes.body":"本文","notes.save":"ノートを保存",
    "notes.empty.title":"ノートがありません","notes.empty.body":"下のフォームでノートを追加する。",
    "notes.earlier":"— 以前の {n} 件は省略 —",
    "export.head":"エクスポート","viewer.close":"閉じる",
    "reindex.head":"埋め込み","reindex.btn":"埋め込みを再構築",
    "reindex.hint":"埋め込みモデル変更後に再構築する",
    "reindex.ok":"{n}/{total} チャンクを再埋め込みしました",
    "viewer.excerpt":"参照箇所","viewer.full_src":"ソース全文を表示","viewer.section":"節:","viewer.cited_chunk":"引用箇所","viewer.match_label":"検出:","viewer.match_fts":"全文","viewer.match_vec":"意味","viewer.match_lex":"語彙",
    "src.refresh":"更新","src.refresh.ok":"ソースを更新しました",
    "src.rename":"タイトルを編集","src.rename.ok":"タイトルを変更しました",
    "src.scope":"検索対象","chat.noscope":"検索対象の資料を1つ以上選んでください",
    "banner.offline":"行灯が消えています — LLM未接続。引用検索のみ動作します。",
    "err.generic":"通信に失敗。サーバ起動を確認。","busy":"生成中…",
    "chat.hint":"ヒント: -語 で除外検索(例: Python -legacy)。/ で質問へ、1/2/3 でペイン切替",
    "a11y.lang":"言語切替","a11y.upload":"ファイルをアップロード","a11y.url":"URL",
    "a11y.questions":"推奨質問","a11y.delsrc":"ソースを削除","a11y.viewsrc":"ソースを表示: ",
    "a11y.citation":"引用 S","a11y.delnote":"ノートを削除","a11y.nbname":"書院名","a11y.ask":"質問","a11y.notetitle":"題","a11y.notebody":"本文"
  },
  en: {
    "app.title":"Shoin","app.lamp.on":"LLM connected","app.lamp.off":"LLM offline",
    "tabs.sources":"Sources","tabs.chat":"Chat","tabs.studio":"Studio",
    "sources.head":"SHELF","sources.empty.title":"The shelf is empty",
    "sources.empty.body":"Add a file or URL to start a cited conversation.",
    "sources.addurl":"Fetch","sources.added":"Source added","src.pages_failed":"⚠ {n} page(s) could not be extracted — index is incomplete","src.embed_short":"⚠ {n}/{total} chunks embedded — semantic search is partial",
    "nb.placeholder":"Name a new notebook","nb.create":"Create","nb.rename":"Rename","nb.delete":"Delete this notebook",
    "nb.empty.title":"No notebooks yet","nb.empty.body":"Name your first notebook to begin.",
    "chat.head":"DESK","chat.placeholder":"Ask your sources…","chat.ask":"Ask","chat.clear":"Clear",
    "chat.empty.title":"Start with a question","chat.empty.body":"Ask anything grounded in your sources.",
    "chat.you":"You","chat.shoin":"Shoin","chat.degraded":"search only",
    "chat.coverage.low":"low citation coverage","chat.invalid":"unverified citations",
    "chat.misattr":"possible wrong source","chat.numeric":"number not in source","chat.unit":"unit differs from source","chat.negation":"possible contradiction with source","chat.confirmed":"grounding confirmed",
    "chat.uncited":"uncited assertions","chat.uncited_supported":"source match — missing citation","chat.degenerate":"possible generation loop","chat.contradict":"contradictory statements",
    "chat.truncated":"possibly truncated answer","chat.truncated_hint":"generation stopped at the token limit",
    "chat.stream_dropped":"answer stream ended early",
    "chat.earlier":"— {n} earlier messages not shown —",
    "studio.head":"STUDIO","studio.briefing":"Briefing","studio.study_guide":"Study guide",
    "studio.faq":"FAQ","studio.timeline":"Timeline","studio.mindmap":"Mind map",
    "studio.empty.title":"No output yet","studio.empty.body":"Use the buttons above to generate content.",
    "studio.savenote":"Save as note","studio.savenote.ok":"Saved as note",
    "notes.head":"NOTES","notes.title":"Title","notes.body":"Body","notes.save":"Save note",
    "notes.empty.title":"No notes yet","notes.empty.body":"Add a note using the form below.",
    "notes.earlier":"— {n} earlier notes not shown —",
    "export.head":"Export","viewer.close":"Close",
    "reindex.head":"Embeddings","reindex.btn":"Rebuild embeddings",
    "reindex.hint":"Rebuild after changing the embedding model",
    "reindex.ok":"Re-embedded {n}/{total} chunks",
    "viewer.excerpt":"Supporting passage","viewer.full_src":"View full source","viewer.section":"Section:","viewer.cited_chunk":"cited here","viewer.match_label":"found:","viewer.match_fts":"full-text","viewer.match_vec":"semantic","viewer.match_lex":"lexical",
    "src.refresh":"Refresh","src.refresh.ok":"Source refreshed",
    "src.rename":"Edit title","src.rename.ok":"Title updated",
    "src.scope":"In scope","chat.noscope":"Select at least one source to ask.",
    "banner.offline":"The lantern is out — no LLM connected. Citation search still works.",
    "err.generic":"Request failed. Is the server running?","busy":"Working…",
    "chat.hint":"Tip: use -word to exclude (e.g. Python -legacy). / focuses, 1/2/3 switches panes",
    "a11y.lang":"Switch language","a11y.upload":"Upload file","a11y.url":"URL",
    "a11y.questions":"Suggested questions","a11y.delsrc":"Delete source","a11y.viewsrc":"View source: ",
    "a11y.citation":"Citation S","a11y.delnote":"Delete note","a11y.nbname":"Notebook name","a11y.ask":"Question","a11y.notetitle":"Title","a11y.notebody":"Body"
  }
};
// Storage can be outright disabled (private-mode restrictions, sandboxed
// iframes, cookies blocked): localStorage access throws SecurityError, and at
// script top level that kills the entire boot — page renders, every control
// dead. Accesses funnel through these guards; failures degrade to in-memory
// no-ops (language falls back per the precedence below; the toggle just
// doesn't persist).
const _lsGet = k => { try{ return localStorage.getItem(k) }catch(e){ return null } };
const _lsSet = (k,v) => { try{ localStorage.setItem(k,v) }catch(e){} };
// Precedence: an explicit toggle click (persisted to localStorage) always wins;
// otherwise the operator's SHOIN_LANG (server-injected into the meta tag below
// as a real 2-letter code — see server.py's _h_ui; an unsubstituted build-time
// placeholder is longer than 2 chars and is deliberately rejected by the
// length check, not a literal-string comparison, so this can't be fooled by
// the placeholder text appearing anywhere else in this file) is the
// configured default; otherwise the browser's own locale; "ja" is the final
// fallback if nothing else applies.
const _serverLang = document.querySelector('meta[name="shoin-lang"]')?.content;
let lang = _lsGet("shoin.lang")
  || (_serverLang && _serverLang.length === 2 ? _serverLang : null)
  || (navigator.language||"ja").slice(0,2);
if (!I18N[lang]) lang = "en";
const t = k => (I18N[lang] && I18N[lang][k]) || I18N.ja[k] || k;
function applyI18n(){
  document.documentElement.lang = lang;
  document.querySelectorAll("[data-i18n]").forEach(el=>{el.textContent=t(el.dataset.i18n);});
  document.querySelectorAll("[data-i18n-ph]").forEach(el=>{el.placeholder=t(el.dataset.i18nPh);});
  document.querySelectorAll("[data-i18n-title]").forEach(el=>{el.title=t(el.dataset.i18nTitle);});
  document.querySelectorAll("[data-i18n-aria]").forEach(el=>{el.setAttribute("aria-label", t(el.dataset.i18nAria));});
  $("#langBtn").textContent = lang==="ja" ? "EN" : "日本語";
  $("#langBtn").setAttribute("aria-label", t("a11y.lang"));
}

/* ---------- helpers ---------- */
const $ = s => document.querySelector(s);
const el = (tag, cls, text) => { const n=document.createElement(tag); if(cls)n.className=cls; if(text!=null)n.textContent=text; return n; };
function embedNote(j){
  // Embed-skip surfacing: when the backend is configured for embeddings
  // (_embedOn from /api/health) but an ingest embedded fewer chunks than it
  // produced — endpoint failure, stored-model mismatch, or a partial batch —
  // the toast must not present the index as complete, the same reason
  // pages_failed is appended. Silent when embeddings are off: there 0
  // embedded is the first-class mode, not a degradation.
  return window._embedOn && j.n_embedded < j.n_chunks
    ? " " + t("src.embed_short").replace("{n}", j.n_embedded).replace("{total}", j.n_chunks)
    : "";
}
function toast(msg){ const x=$("#toast"); x.textContent=msg; x.style.display="block";
  clearTimeout(x._t); x._t=setTimeout(()=>x.style.display="none", 6000); }
async function api(path, opts){
  const r = await fetch(path, opts);
  if (!r.ok){
    let code=r.status, msg=t("err.generic");
    try{ const j=await r.json(); code=j.error.code; msg=j.error.message; }catch(_e){/* keep defaults */}
    throw new Error(`[${code}] ${msg}`);
  }
  return r;
}
const jpost = (path, body) => api(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});

/* ---------- constants ---------- */
// Mirrors citation.COVERAGE_LOW (Python). An answer citing fewer than this
// fraction of its sources may be ignoring retrieved evidence; the CLI and the
// Markdown export warn on the same threshold (v0.2.138) — keep the three in sync.
const COVERAGE_LOW = 0.5;

/* ---------- state ---------- */
let notebooks = [], cur = null, srcIndex = new Map(); // source_id -> {s, title}
// Per-notebook selection scope for ask (source_ids, v0.2.632): srcSel holds
// the source ids a question is scoped to. knownIds tracks which ids have been
// seen, so a newly added source defaults to checked while an explicit uncheck
// survives the re-renders every mutation triggers via openNotebook().
const srcSel = new Set(), knownIds = new Set(); let selNb = -1;
// Stashes an uncommitted rename that's about to be torn down by a rebuild
// the user themselves triggered from the SAME row (clicking refresh while
// mid-rename) -- renderNotebook()'s own document.activeElement-based
// preservation can't see this case, because clicking a sibling button moves
// focus OFF the rename input before the rebuild ever runs. See the refresh
// button's onclick handler and renderNotebook()'s pendingRename logic.
let externalPendingRename = null;

/* ---------- notebooks & sources ---------- */
async function loadNotebooks(){
  try{
  const j = await (await api("/api/notebooks")).json();
  notebooks = j.notebooks || [];
  const ul = $("#nbList"); ul.replaceChildren();
  for (const nb of notebooks){
    const li = el("li"); li.className = cur && cur.id===nb.id ? "cur":"";
    const name = el("span","name", nb.name);
    const c = el("span","c", `${nb.counts.sources}册`);
    const ren = el("button","ren","✎"); ren.type="button"; ren.title=t("nb.rename"); ren.setAttribute("aria-label",t("nb.rename"));
    ren.onclick = async ev=>{
      ev.stopPropagation();
      const newName = prompt(t("nb.rename"), nb.name);
      if (!newName || !newName.trim()) return;
      ren.disabled = true; del.disabled = true;
      try{
        await api(`/api/notebooks/${nb.id}`, {method:"PATCH",
          headers:{"Content-Type":"application/json"}, body:JSON.stringify({name:newName.trim()})});
        if (cur && cur.id===nb.id) cur.name = newName.trim();
        loadNotebooks(); renderNotebook();
      }catch(e){ toast(e.message); ren.disabled = false; del.disabled = false; }
    };
    const del = el("button","ren del",null); del.textContent="×"; del.type="button";
    del.title=t("nb.delete"); del.setAttribute("aria-label", t("nb.delete"));
    del.onclick = async ev=>{
      ev.stopPropagation();
      if (!confirm(t("nb.delete"))) return;
      ren.disabled = true; del.disabled = true;
      try{
        await api(`/api/notebooks/${nb.id}`, {method:"DELETE"});
        if (cur && cur.id===nb.id) cur=null;
        loadNotebooks();
      }catch(e){ toast(e.message); ren.disabled = false; del.disabled = false; }
    };
    li.append(name,c,ren,del);
    li.onclick = () => openNotebook(nb.id);
    li.tabIndex=0; li.onkeydown=e=>{
      // Only the row itself may trigger the row action: a keydown bubbling up
      // from one of the row's own buttons (✎/×) must not fire it — the
      // preventDefault() below would ALSO cancel the focused button's native
      // Enter/Space activation, making that button keyboard-unreachable and
      // running openNotebook when the user asked for rename/delete.
      if (e.target!==li) return;
      if(e.key==="Enter"||e.key===" "){ e.preventDefault(); openNotebook(nb.id); } };
    ul.append(li);
  }
  if (!notebooks.length){
    cur = null; renderNotebook();
    const e0 = el("div","empty"); e0.append(el("b",null,t("nb.empty.title")), el("span",null,t("nb.empty.body")));
    ul.append(e0);
  } else if (!cur){ openNotebook(notebooks[0].id); }
  }catch(e){ toast(e.message); }
}
// Requests can resolve out of order (click notebook A, then B; B's response
// arrives first) — last-write-wins would then leave the user looking at the
// notebook they did NOT select last.  Same _sealSeq-style guard openSeal uses
// for its async excerpt probes: the newest call wins; stale resolves discard.
let _nbSeq = 0;
async function openNotebook(id){
  const seq = ++_nbSeq;
  try{
    const j = await (await api(`/api/notebooks/${id}`)).json();
    if (seq !== _nbSeq) return;
    // Boundary normalization: inside render* every collection field exists,
    // so raw .forEach/.length reads and ?./||[] guards stay interchangeable.
    cur = {sources:[], messages:[], notes:[], studio:[], ...j};
    renderNotebook(); loadNotebooks();
  }catch(e){ if (seq === _nbSeq) toast(e.message); }
}
// Enters inline-rename mode for a source row. Factored out of the dblclick
// handler so renderNotebook() can also call it to RESTORE an in-progress,
// uncommitted edit after a re-render — every unrelated write in the app
// (note add/delete, upload, studio generate, clear-chat, ...) calls
// openNotebook() on success, which used to unconditionally tear down and
// rebuild #srcList, silently discarding whatever the user was mid-typing in
// a rename input elsewhere on the page with no warning.
function startSourceRename(s, tt, row, prefillValue){
  const nb = cur;  // capture notebook reference; cur may drift if user switches
  const input = document.createElement("input");
  input.type = "text"; input.className = "src-rename"; input.dataset.srcId = s.id;
  input.value = prefillValue !== undefined ? prefillValue : s.title;
  // Prevent clicks inside the input from propagating to row.onclick (showSource)
  input.onclick = e => e.stopPropagation();
  tt.replaceChildren(input, el("div","k", s.kind));
  input.focus(); input.select();
  let committed = false;
  const commit = async () => {
    if (committed) return; committed = true;
    const newTitle = input.value.trim();
    if (!newTitle || newTitle === s.title) { openNotebook(nb.id); return; }
    input.disabled = true;
    try {
      await api(`/api/sources/${s.id}`, {method:"PATCH", body:JSON.stringify({title:newTitle})});
      toast(t("src.rename.ok")); openNotebook(nb.id);
    } catch(e) { toast(e.message); openNotebook(nb.id); }
    finally { input.disabled = false; }
  };
  // relatedTarget check: if focus moves to another element within this row
  // (e.g. the delete or refresh button), skip blur-commit so the button click
  // fires normally on its own; the button's onclick rebuilds the DOM anyway.
  input.onblur = e => { if (row.contains(e.relatedTarget)) return; commit(); };
  input.onkeydown = e => {
    if (e.key==="Enter"){e.preventDefault();commit();}
    if (e.key==="Escape"){
      // Mark committed (so onblur's commit() below is a no-op) and blur
      // BEFORE reloading: renderNotebook()'s "preserve an in-progress
      // rename across an unrelated rebuild" logic keys off whether this
      // input is still document.activeElement, and since openNotebook()
      // is async, it would otherwise still be focused when the reload
      // completes — resurrecting the exact edit the user just cancelled
      // with the discarded text still in it.
      committed = true;
      input.blur();
      openNotebook(nb.id);
    }
  };
  return input;
}
function renderNotebook(){
  $("#nbTitle").textContent = cur ? `${t("app.title")} — ${cur.name}` : t("app.title");
  $("#srcAdders").hidden = !cur;
  const list = $("#srcList");
  // Preserve an in-progress, uncommitted rename edit across this rebuild (see
  // startSourceRename's comment for why this is needed).
  const activeRename = document.activeElement;
  let pendingRename = null;
  if (activeRename && activeRename.classList?.contains("src-rename") && list.contains(activeRename)) {
    pendingRename = {
      srcId: Number(activeRename.dataset.srcId),
      value: activeRename.value,
      selStart: activeRename.selectionStart,
      selEnd: activeRename.selectionEnd,
    };
    // Removing a focused element from the DOM fires a native blur event.
    // startSourceRename's onblur handler would treat that as the user
    // navigating away and auto-commit the (uncommitted) edit via PATCH —
    // firing right as this rebuild is about to restore the same edit,
    // racing its own restoration. Detach the old handlers first: this input
    // is being replaced by a fresh one below with fresh handlers regardless.
    activeRename.onblur = null;
    activeRename.onkeydown = null;
  } else if (externalPendingRename) {
    // A sibling control in the same row (e.g. the refresh button) was
    // clicked while a rename was in progress, moving focus off the input
    // before this rebuild ran -- document.activeElement can't see it, but
    // the click handler stashed it here. See externalPendingRename's own
    // comment and the refresh button's onclick handler.
    pendingRename = externalPendingRename;
  }
  externalPendingRename = null;  // consume: a stash is only ever restored once
  list.replaceChildren();
  srcIndex.clear();
  if (cur){
    if (cur.id !== selNb){ selNb = cur.id; srcSel.clear(); knownIds.clear(); }
    const liveIds = new Set();
    cur.sources.forEach(s=>{
      liveIds.add(s.id);
      if (!knownIds.has(s.id)){ knownIds.add(s.id); srcSel.add(s.id); }
    });
    for (const id of [...knownIds]) if (!liveIds.has(id)){ knownIds.delete(id); srcSel.delete(id); }
    cur.sources.forEach((s,i)=>{
      srcIndex.set(s.id, {s:i+1, title:s.title});
      const row = el("div","src");
      const cb = el("input","src-sel");
      cb.type = "checkbox";
      cb.checked = srcSel.has(s.id);
      cb.setAttribute("aria-label", t("src.scope"));
      cb.setAttribute("title", t("src.scope"));
      cb.onclick = ev => ev.stopPropagation();
      cb.onchange = () => { if (cb.checked) srcSel.add(s.id); else srcSel.delete(s.id); updateScopeInfo(); };
      row.append(cb);
      row.append(el("span","no",`S${i+1}`));
      const tt = el("span","t", s.title); tt.append(el("div","k", s.kind));
      // Inline title editing: double-click turns the span into an input
      tt.ondblclick = ev => { ev.stopPropagation(); startSourceRename(s, tt, row); };
      tt.setAttribute("title", t("src.rename"));
      row.append(tt);
      // Refresh button: URL sources always, file sources while their
      // recorded path still exists (server-computed `refreshable`).
      if (s.refreshable) {
        const ref = el("button","src-act","↻"); ref.setAttribute("aria-label", t("src.refresh"));
        ref.setAttribute("title", t("src.refresh"));
        ref.onclick = async ev => { ev.stopPropagation();
          // Clicking this button moves focus off any in-progress rename
          // input in the SAME row before openNotebook()'s rebuild runs, so
          // renderNotebook()'s document.activeElement-based preservation
          // never sees it -- unlike unrelated background rebuilds (note
          // add/delete, upload, ...), this one is triggered by a sibling
          // control the user just clicked, silently discarding whatever
          // they were mid-typing with no commit, no restore, no toast. The
          // source isn't going away (unlike the delete button, where
          // discarding a pending rename is correct since it'd be moot) --
          // stash the uncommitted value so renderNotebook() can restore it
          // after the refresh completes, same as it does for those other
          // rebuilds.
          const renameInput = tt.querySelector("input.src-rename");
          if (renameInput) {
            const v = renameInput.value.trim();
            if (v && v !== s.title) {
              externalPendingRename = {
                srcId: s.id, value: renameInput.value,
                selStart: renameInput.selectionStart, selEnd: renameInput.selectionEnd,
              };
            }
            // Detach handlers: this input is about to be torn down by the
            // rebuild below regardless: no stale commit()/blur() should
            // fire on it afterward.
            renameInput.onblur = null; renameInput.onkeydown = null;
          }
          ref.disabled = true; ref.textContent = "…";
          try { const j = await (await api(`/api/sources/${s.id}/refresh`,{method:"POST"})).json();
            toast(t("src.refresh.ok") + (j.pages_failed ? " " + t("src.pages_failed").replace("{n}", j.pages_failed) : "") + embedNote(j));
            openNotebook(cur.id); }
          catch(e){ toast(e.message); ref.disabled=false; ref.textContent="↻"; } };
        row.append(ref);
      }
      const del = el("button","src-act",null); del.textContent = "×"; del.setAttribute("aria-label",t("a11y.delsrc"));
      del.onclick = async ev=>{ ev.stopPropagation();
        del.disabled = true;
        try{ await api(`/api/sources/${s.id}`,{method:"DELETE"}); openNotebook(cur.id);}
        catch(e){ toast(e.message); del.disabled = false; } };
      row.append(del);
      row.onclick = () => { if (tt.querySelector("input.src-rename")) return; showSource(s.id, s.title); };
      row.tabIndex = 0;
      row.setAttribute("role", "button");
      row.setAttribute("aria-label", t("a11y.viewsrc") + s.title);
      row.onkeydown = e => {
        // Same bubbled-keydown guard as the notebook rows: a keydown from the
        // row's own ↻/× buttons would have its native activation cancelled by
        // preventDefault() below (button unreachable by keyboard + wrong
        // command run), and Enter inside the in-place rename input would fire
        // showSource mid-commit. Fire only when the row itself is the target.
        if (e.target !== row) return;
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); showSource(s.id, s.title); } };
      list.append(row);
      // Restore an in-progress rename AFTER the row is attached to the live
      // DOM — input.focus() inside startSourceRename() is a no-op on a
      // detached element, so this must run after list.append(row) above.
      if (pendingRename && pendingRename.srcId === s.id) {
        const restored = startSourceRename(s, tt, row, pendingRename.value);
        restored.setSelectionRange(pendingRename.selStart, pendingRename.selEnd);
      }
    });
    $("#srcEmpty").hidden = (cur.sources?.length||0)>0;
    updateScopeInfo();
    renderChatHistory(); renderStudio(); renderNotes(); refreshQuestions();
    $("#exMd").href = `/api/notebooks/${cur.id}/export?format=md`;
    $("#exBib").href = `/api/notebooks/${cur.id}/export?format=bibtex`;
    $("#exRis").href = `/api/notebooks/${cur.id}/export?format=ris`;
  } else {
    // No notebook selected: clear all dependent panes so deleted-notebook content
    // does not persist in the UI after the last notebook is removed.
    $("#srcEmpty").hidden = true;
    $("#chat").replaceChildren();
    $("#chatEmpty").hidden = false;
    $("#clearChat").hidden = true;
    $("#degBadge").hidden = true;
    $("#studioOut").replaceChildren();
    $("#noteList").replaceChildren();
    $("#qs").replaceChildren();
    // Export links keep their href from the last-open notebook otherwise:
    // a stale /api/notebooks/{deleted-id}/export link stays visible and
    // clickable, 404ing silently on click with no explanation to the user.
    $("#exMd").removeAttribute("href");
    $("#exBib").removeAttribute("href");
    $("#exRis").removeAttribute("href");
  }
}

/* ---------- citations: 蔵書印 chips ---------- */
function renderWithSeals(container, text, report){
  container.replaceChildren();
  // Match citation.py's NFKC-before-regex behavior: a JP-first local model
  // commonly emits full-width citation brackets/digits (［Ｓ１］), which the
  // backend already normalizes and verifies, but the ASCII-only \[ \] \d
  // regexes below would otherwise leave as dead, unstyled text.
  text = text.normalize("NFKC");
  const invalid = new Set((report && report.invalid) || []);
  const misattr = new Set((report && report.misattributed) || []);
  const numeric = new Set((report && report.numeric_mismatch) || []);
  const unit = new Set((report && report.unit_mismatch) || []);
  const negation = new Set((report && report.negation_mismatch) || []);
  const confirmed = new Set((report && report.confirmed) || []);
  const srcMap = (report && report.source_map) || {};
  const idMap = (report && report.source_id_map) || null;
  const excerpts = (report && report.source_excerpts) || {};
  const contexts = (report && report.source_contexts) || {};
  const chunkIds = (report && report.source_chunk_ids) || {};
  const details = (report && report.source_detail) || {};
  // Match a whole bracket group, then pull every S-number inside it so that
  // combined citations like "[S1, S2]" render as two seals (matches backend).
  const reB = /\[([^\[\]]+)\]/g; let last = 0, m;
  while ((m = reB.exec(text))){
    const nums = [...m[1].matchAll(/[Ss]\s*(\d+)/g)].map(x => Number(x[1]));
    if (!nums.length) continue;           // not a citation bracket: leave as text
    if (m.index > last) container.append(document.createTextNode(text.slice(last, m.index)));
    nums.forEach((n, i) => {
      if (i) container.append(document.createTextNode(" "));
      const cls = invalid.has(n) ? " bad" : ((misattr.has(n)||numeric.has(n)||unit.has(n)||negation.has(n)) ? " mis" : (confirmed.has(n) ? " ok" : ""));
      const chip = el("button","seal" + cls, `S${n}`);
      chip.type="button";
      const name = srcMap[`S${n}`] || "";
      const section = contexts[`S${n}`] || "";
      const nameSec = name + (section ? " › " + section : "");
      chip.title = invalid.has(n) ? t("chat.invalid")
        : misattr.has(n) ? t("chat.misattr") + (name ? " — " + nameSec : "")
        : numeric.has(n) ? t("chat.numeric") + (name ? " — " + nameSec : "")
        : unit.has(n) ? t("chat.unit") + (name ? " — " + nameSec : "")
        : negation.has(n) ? t("chat.negation") + (name ? " — " + nameSec : "")
        : confirmed.has(n) ? t("chat.confirmed") + (name ? " — " + nameSec : "")
        : nameSec;
      chip.setAttribute("aria-label", t("a11y.citation") + n + (name ? ": " + nameSec : ""));
      chip.onclick = () => openSeal(n, srcMap, idMap, excerpts, contexts, chunkIds, details);
      container.append(chip);
    });
    last = reB.lastIndex;
  }
  if (last < text.length) container.append(document.createTextNode(text.slice(last)));
}
let _sealSeq = 0;
async function openSeal(n, srcMap, idMap, excerpts, contexts, chunkIds, details){
  // Prefer the authoritative source_id_map (avoids title collisions); fall back to title scan.
  const sid = idMap && idMap[`S${n}`];
  const title = srcMap[`S${n}`];
  const excerpt = (excerpts && excerpts[`S${n}`]) || null;
  const section = (contexts && contexts[`S${n}`]) || null;
  const cited = (chunkIds && chunkIds[`S${n}`]) || null;
  const detail = (details && details[`S${n}`]) || null;
  if (sid){ showSource(sid, title || `S${n}`, excerpt, section, cited, detail); return; }
  const candidates = [];
  for (const [id, info] of srcIndex){ if (info.title === title) candidates.push(id); }
  if (candidates.length === 1){ showSource(candidates[0], title, excerpt, section, cited, detail); return; }
  if (!candidates.length){ if (title) toast(title); return; }
  // Title collision (only reachable on pre-source_id_map reports): the first
  // match is not necessarily the cited source, and opening the wrong one is
  // silent misattribution on the surface built to prevent it.  When the stored
  // excerpt is available, pick the candidate whose chunks actually contain it —
  // the same provable check renderFullSource applies to chunk ids (v0.2.230).
  // _sealSeq guards the async probes against a second seal click racing in.
  const seq = ++_sealSeq;
  if (excerpt){
    for (const id of candidates){
      try{
        const j = await (await api(`/api/sources/${id}/text`)).json();
        if (seq !== _sealSeq) return;
        if ((j.chunks || []).some(c => excerpt.includes(String(c.text).slice(0, 24)))){
          showSource(id, title, excerpt, section, cited, detail); return;
        }
      }catch(e){ /* probe failed — try the next candidate */ }
    }
  }
  if (seq === _sealSeq) showSource(candidates[0], title, excerpt, section, cited, detail);
}
let _srcAbort = null, _viewerOpener = null;
function closeViewer(){
  $("#viewer").classList.remove("open");
  if (_viewerOpener) { _viewerOpener.focus(); _viewerOpener = null; }
}
// Render a source's full text chunk-by-chunk, marking the chunks the answer was
// actually grounded in (citation_report.source_chunk_ids, v0.2.139) and scrolling
// to the first one. This is the last mile of "verifiable citation": the reader
// sees the cited wording in its original position, not just a detached excerpt.
// citedIds absent (old persisted report / Studio output) → plain text, unmarked.
function renderFullSource(container, chunks, citedIds, excerpt){
  container.replaceChildren();
  const marked = new Set(citedIds || []);
  // chunks.id is a plain rowid (no AUTOINCREMENT) — a refreshed source's new
  // chunks can REUSE the ids an old report stored, so an id match alone can
  // pin the "cited here" mark on text the citation never saw (silent
  // misattribution). When the report's stored excerpt is available, only mark
  // a chunk whose head actually appears in it (v0.2.230); without an excerpt
  // there is nothing to verify against, so the old id-only behavior stays.
  const provable = typeof excerpt === "string" && excerpt.length > 0;
  let first = null;
  (chunks || []).forEach((c, i) => {
    if (i) container.append(el("div","chunk-sep","⋯"));
    const isCited = marked.has(c.id) &&
      (!provable || excerpt.includes(String(c.text).slice(0, 24)));
    const block = el("div", isCited ? "src-chunk cited-chunk" : "src-chunk", c.text);
    if (isCited){
      block.prepend(el("div","cited-label", t("viewer.cited_chunk")));
      if (!first) first = block;
    }
    container.append(block);
  });
  // Bring the first cited passage into view so a long document doesn't require
  // manual scanning. Guarded: no cited chunks (or an old report) → no scroll.
  if (first) first.scrollIntoView({block:"nearest"});
}
async function showSource(id, title, excerpt, section, citedIds, detail){
  if (_srcAbort) _srcAbort.abort();
  _srcAbort = new AbortController();
  const sig = _srcAbort.signal;
  _viewerOpener = document.activeElement;
  $("#viewerTitle").textContent = title;
  const vt = $("#viewerText");
  vt.replaceChildren();
  // Section breadcrumb (v0.2.130): shows WHICH section of the source the cited
  // passage came from. Rendered above the excerpt when present; absent for old
  // persisted reports or sources with no heading structure.
  if (section){
    vt.append(el("div","section-label", t("viewer.section") + " " + section));
  }
  // Retrieval provenance (v0.2.228): which channel surfaced this source —
  // BM25 rank and/or vector rank, plus the term-presence signal. A source
  // found only semantically (no full-text rank) is exactly the class where
  // unsupported claims live, so the why-it-surfaced signal sits next to the
  // what-it-said excerpt. Absent on old persisted reports.
  if (detail){
    const found = [];
    if (detail.rrf_bm25_rank) found.push(t("viewer.match_fts") + " #" + detail.rrf_bm25_rank);
    if (detail.rrf_vec_rank) found.push(t("viewer.match_vec") + " #" + detail.rrf_vec_rank);
    if (detail.lex) found.push(t("viewer.match_lex") + " " + Number(detail.lex).toFixed(2));
    if (found.length) vt.append(el("div","section-label", t("viewer.match_label") + " " + found.join(" + ")));
  }
  if (excerpt){
    // Show the retrieved passage immediately — no network round-trip needed.
    vt.append(el("div","excerpt-label", t("viewer.excerpt")));
    vt.append(el("div","excerpt", excerpt));
    // Lazy-load the full source text only when the user expands the details.
    const det = document.createElement("details"); det.className="full-src";
    const sum = document.createElement("summary"); sum.textContent = t("viewer.full_src");
    det.append(sum);
    det.addEventListener("toggle", async () => {
      if (!det.open || det.dataset.loaded) return;
      det.dataset.loaded = "1";
      // One body element per <details>: reused on retry so a failed fetch
      // can never leave a stale second placeholder behind.
      let body = det.querySelector(".full-body");
      if (!body){ body = el("div","full-body", "…"); det.append(body); }
      body.textContent = "…";
      try{
        const j = await (await api(`/api/sources/${id}/text`, {signal:sig})).json();
        if (sig.aborted) return;
        renderFullSource(body, j.chunks, citedIds, excerpt);
      }catch(e){
        // Failure must clear `loaded` — collapse→reopen is the retry gesture,
        // and keeping the flag set would pin the error text on permanently.
        if (!sig.aborted){ delete det.dataset.loaded; body.textContent = e.message; }
      }
    });
    vt.append(det);
    $("#viewer").classList.add("open");
    $("#viewerClose").focus();
  } else {
    // No excerpt (old persisted message or Studio output): fetch immediately.
    try{
      const j = await (await api(`/api/sources/${id}/text`, {signal:sig})).json();
      if (sig.aborted) return;
      renderFullSource(vt, j.chunks, citedIds);
      $("#viewer").classList.add("open");
      $("#viewerClose").focus();
    }catch(e){ if (!sig.aborted) toast(e.message); }
  }
}
$("#viewerClose").onclick = () => closeViewer();
$("#viewer").onclick = e => { if (e.target.id==="viewer") closeViewer(); };
// Focus trap: keep Tab/Shift-Tab within the dialog when open
$("#viewer").addEventListener("keydown", e=>{
  if (!$("#viewer").classList.contains("open")) return;
  if (e.key==="Escape"){ closeViewer(); return; }
  if (e.key!=="Tab") return;
  const focusable = Array.from($("#viewer").querySelectorAll(
    'button:not([disabled]),input:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])'));
  if (!focusable.length) return;
  const first = focusable[0], last = focusable[focusable.length-1];
  if (e.shiftKey && document.activeElement===first){ e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement===last){ e.preventDefault(); first.focus(); }
});
document.addEventListener("keydown", e=>{ if(e.key==="Escape") closeViewer(); });

/* ---------- chat ---------- */
// The badge row beneath an assistant message or Studio card. Lives in one
// place because it is the SAME chain for all three surfaces (chat SSE, chat
// history, Studio) — when a new check lands, one copy per surface is how flags
// end up warning in one place and staying silent in another (v0.2.234-235).
function reportBadges(c, report){
  if (report.degraded) c.append(el("span","badge dim", t("chat.degraded")));
  if (report.invalid && report.invalid.length){
    const b = el("span","badge err", `⚠ ${t("chat.invalid")}: ` + report.invalid.map(i=>"S"+i).join(", "));
    c.append(b);
  }
  if (report.misattributed && report.misattributed.length){
    const sugg = report.misattributed_suggested || {};
    c.append(el("span","badge err", `⚠ ${t("chat.misattr")}: ` + report.misattributed.map(i=>"S"+i+(sugg["S"+i]?"→"+sugg["S"+i]:"")).join(", ")));
  }
  if (report.numeric_mismatch && report.numeric_mismatch.length){
    c.append(el("span","badge err", `⚠ ${t("chat.numeric")}: ` + report.numeric_mismatch.map(i=>"S"+i).join(", ")));
  }
  if (report.unit_mismatch && report.unit_mismatch.length){
    c.append(el("span","badge err", `⚠ ${t("chat.unit")}: ` + report.unit_mismatch.map(i=>"S"+i).join(", ")));
  }
  if (report.negation_mismatch && report.negation_mismatch.length){
    c.append(el("span","badge err", `⚠ ${t("chat.negation")}: ` + report.negation_mismatch.map(i=>"S"+i).join(", ")));
  }
  if (report.confirmed && report.confirmed.length){
    c.append(el("span","badge dim", `✓ ${t("chat.confirmed")}: ` + report.confirmed.map(i=>"S"+i).join(", ")));
  }
  if (report.uncited && report.uncited.length){
    const sup = new Set(report.uncited_supported || []);
    const supSrc = report.uncited_supported_source || {};
    const b = el("span","badge warn", `⚠ ${t("chat.uncited")}: ${report.uncited.length}`);
    // Grounded uncited = citation omission; ungrounded = the dangerous kind.
    b.title = report.uncited.map(s => sup.has(s) ? s + ` [${t("chat.uncited_supported")}→${supSrc[s] || ""}]` : s).join("\n");
    c.append(b);
  }
  if (report.degenerate && report.degenerate.length){
    const b = el("span","badge warn", `⚠ ${t("chat.degenerate")}: ${report.degenerate.length}`);
    b.title = report.degenerate.join("\n");
    c.append(b);
  }
  if (report.self_contradiction && report.self_contradiction.length){
    const b = el("span","badge warn", `⚠ ${t("chat.contradict")}: ${report.self_contradiction.length}`);
    b.title = report.self_contradiction.join("\n");
    c.append(b);
  }
  // report.truncated = the LLM answered to MAX_TOKENS (finish_reason "length").
  // The text is genuine but clipped mid-generation — warn rather than present
  // a truncated answer as complete (v0.2.245).
  if (report.truncated){
    const b = el("span","badge warn", `⚠ ${t("chat.truncated")}`);
    b.title = t("chat.truncated_hint");
    c.append(b);
  }
  if (typeof report.coverage==="number" && report.cited && report.cited.length && report.coverage < COVERAGE_LOW){
    c.append(el("span","badge warn", t("chat.coverage.low")));
  }
}
function addMsg(role, body, report){
  const wrap = el("div","msg "+role);
  wrap.append(el("div","who", role==="user" ? t("chat.you") : t("chat.shoin")));
  const bd = el("div","body");
  renderWithSeals(bd, body, report);
  wrap.append(bd);
  if (report && role!=="user"){
    const c = el("div","cites");
    reportBadges(c, report);
    if (c.children.length) wrap.append(c);
  }
  $("#chat").append(wrap);
  $("#chat").scrollTop = $("#chat").scrollHeight;
  return bd;
}
function renderChatHistory(){
  $("#degBadge").hidden = true;
  $("#chat").replaceChildren();
  // Disclose the server-side history cap (messages_omitted) rather than
  // silently rendering a partial log — same honesty convention as the
  // budget-cut marker and truncated badge.
  if (cur.messages_omitted)
    $("#chat").prepend(el("div","empty", t("chat.earlier").replace("{n}", cur.messages_omitted)));
  (cur.messages||[]).forEach(m=>addMsg(m.role==="user"?"user":"ai", m.body, m.report));
  const hasChat = (cur.messages||[]).length > 0;
  $("#chatEmpty").hidden = hasChat;
  $("#clearChat").hidden = !hasChat;
}
$("#clearChat").onclick = async ()=>{
  if (!cur) return;
  const btn = $("#clearChat");
  btn.disabled = true;
  try{
    await api(`/api/notebooks/${cur.id}/messages`, {method:"DELETE"});
    openNotebook(cur.id);
  }catch(e){ toast(e.message); }
  finally{ btn.disabled = false; }
};
async function refreshQuestions(){
  $("#qs").replaceChildren();
  if (!cur || !cur.sources?.length || !window._llmOn) return;
  const nbId = cur.id;  // capture before await to detect notebook switches
  try{
    const j = await (await api(`/api/notebooks/${nbId}/questions`)).json();
    if (!cur || cur.id !== nbId) return;  // notebook changed while waiting
    (j.questions || []).forEach(q=>{
      const b = el("button","q-chip", q); b.type="button";
      b.onclick = ()=>{ $("#askInput").value=q; $("#askInput").focus(); };
      $("#qs").append(b);
    });
  }catch(_e){ /* suggestions are best-effort */ }
}
function scopeSelection(){
  const live = (cur && cur.sources ? cur.sources : []).map(s=>s.id);
  return {live, sel: live.filter(id=>srcSel.has(id))};
}
function scopedIds(sel, live){
  // Absent field == whole notebook (the API treats an empty source_ids the
  // same); only a partial selection is worth sending — null means "omit".
  return sel.length && sel.length < live.length ? sel : null;
}
function updateScopeInfo(){
  const info = $("#srcSelInfo"); if (!info) return;
  if (!cur || !cur.sources || !cur.sources.length){ info.textContent = ""; return; }
  info.textContent = `${scopeSelection().sel.length}/${cur.sources.length}`;
}
$("#askForm").onsubmit = async e=>{
  e.preventDefault();
  if (!cur) return;
  const nbId = cur.id;
  const q = $("#askInput").value.trim(); if(!q) return;
  const scope = scopeSelection();
  // Sources exist but every one is unchecked — asking would silently query the
  // whole notebook (source_ids:[] is unscoped), the opposite of the user's
  // intent, so refuse before the input is cleared.
  if (scope.live.length && !scope.sel.length){ toast(t("chat.noscope")); return; }
  $("#askInput").value=""; $("#chatEmpty").hidden=true;
  addMsg("user", q, null);
  const bd = addMsg("ai", "", null);
  bd.append(el("span","spin", t("busy")));
  $("#askBtn").disabled = true;
  let acc = "", gotDone = false, failed = false;
  // Baseline for the drop-recovery poll below: the last persisted assistant
  // body BEFORE this ask lands — captured while `cur` still holds this
  // notebook's detail (v0.2.665).
  const _asst0 = (cur && cur.messages || []).filter(m=>m.role==="assistant");
  const _baseAsstBody = _asst0.length ? _asst0[_asst0.length-1].body : null;
  try{
    const ids = scopedIds(scope.sel, scope.live);
    const r = await jpost(`/api/notebooks/${nbId}/ask`, ids ? {question:q, source_ids:ids} : {question:q});
    const reader = r.body.getReader(); const dec = new TextDecoder();
    let buf = "";
    for(;;){
      const {done, value} = await reader.read(); if (done) break;
      buf += dec.decode(value, {stream:true});
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0){
        const frame = buf.slice(0,i); buf = buf.slice(i+2);
        let ev="message", data="";
        for (const line of frame.split("\n")){
          if (line.startsWith("event:")) ev = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (!data) continue;
        let j; try{ j = JSON.parse(data); }catch(_e){ continue; }
        // meta frames carry the source list, but every field is also present in
        // the done frame's report (source_map/source_id_map) — nothing to store.
        if (ev==="delta"){ acc += j.text ?? ""; bd.textContent = acc; $("#chat").scrollTop = $("#chat").scrollHeight; }
        else if (ev==="done"){
          gotDone = true;
          const rpt = j.report || {};
          // Pane-head degraded badge: the done frame carries `degraded` — this
          // is the per-answer retrieval-only signal (LLM nominally on but the
          // call errored), distinct from the global banner's "LLM off".
          $("#degBadge").hidden = !j.degraded;
          renderWithSeals(bd, acc, rpt);
          const wrap = bd.parentElement, c = el("div","cites");
          reportBadges(c, rpt);
          if (c.children.length) wrap.append(c);
          $("#chat").scrollTop = $("#chat").scrollHeight;
        }
        else if (ev==="error"){
          // The server can fail mid-stream: without this branch the partial
          // answer just freezes with zero signal (the catch path only sees
          // network-level errors, not an SSE "error" frame).
          failed = true;
          toast(j.message || j.code || "");
        }
      }
    }
    if (!gotDone && !failed){
      // Stream ended without a done frame (a proxy or network cut): the server
      // keeps generating after a client drop and persists the COMPLETE answer
      // once finished (v0.2.665), so poll briefly for a NEW last assistant
      // row — a single immediate fetch can race the persist and freeze on the
      // previous turn's message.
      let restored = false;
      for (let tries=0; tries<10 && !restored; tries++){
        if (tries) await new Promise(r=>setTimeout(r,2000));
        try{
          const d = await (await api(`/api/notebooks/${nbId}`)).json();
          const asst = (d.messages||[]).filter(m=>m.role==="assistant");
          const last = asst[asst.length-1];
          if (last && last.body !== _baseAsstBody){
            acc = last.body || "";
            bd.replaceChildren();
            renderWithSeals(bd, acc, last.report || null);
            const c = el("div","cites"); reportBadges(c, last.report || {});
            if (c.children.length) bd.parentElement.append(c);
            $("#degBadge").hidden = !(last.report && last.report.degraded);
            restored = true;
          }
        }catch(_e){ /* keep polling — a transient fetch failure retries like the first */ }
      }
      if (!restored){ if (!acc) bd.replaceChildren(); toast(t("chat.stream_dropped")); }
    }
  }catch(err){ bd.textContent=""; toast(err.message); }
  finally{
    $("#askBtn").disabled = false;
    // If the stream closed before any delta/done event (e.g. server error after meta),
    // bd still holds the spinner — clear it so it doesn't linger permanently.
    if (!acc) bd.replaceChildren();
  }
};

/* ---------- studio & notes ---------- */
const KINDS = ["briefing","study_guide","faq","timeline","mindmap"];
function buildKindButtons(){
  const k = $("#kinds"); k.replaceChildren();
  KINDS.forEach(kind=>{
    const b = el("button","btn", t("studio."+kind)); b.type="button";
    b.onclick = async ()=>{
      if (!cur || !cur.sources?.length) return;
      b.disabled = true; const old=b.textContent; b.textContent=t("busy");
      try{ await jpost(`/api/notebooks/${cur.id}/studio`, {kind}); openNotebook(cur.id); }
      catch(e){ toast(e.message); }
      finally{ b.disabled=false; b.textContent=old; }
    };
    k.append(b);
  });
}
function renderStudio(){
  const out = $("#studioOut"); out.replaceChildren();
  const items = cur.studio||[];
  if (!items.length){
    const e0 = el("div","empty"); e0.append(el("b",null,t("studio.empty.title")), el("span",null,t("studio.empty.body")));
    out.append(e0); return;
  }
  items.forEach(o=>{
    const card = el("div","card");
    const h = el("h4",null,t("studio."+o.kind));
    if (o.report) reportBadges(h, o.report);
    card.append(h);
    const bd = el("div","bd"); renderWithSeals(bd, o.body, o.report); card.append(bd);
    // REQ-103's "studio output -> note": without this button the only path was
    // manual copy-paste into the note form — the spec capability existed in
    // name only. Saves via the same POST /notes endpoint the form uses; the
    // raw body (not the seal-rendered DOM) is what a note should store.
    const sn = el("button","btn",t("studio.savenote"));
    sn.setAttribute("aria-label", t("studio.savenote"));
    sn.onclick = async ()=>{ sn.disabled = true;
      try{
        await jpost(`/api/notebooks/${cur.id}/notes`, {title: t("studio."+o.kind), body: o.body});
        toast(t("studio.savenote.ok")); openNotebook(cur.id);
      }catch(e){ toast(e.message); } finally{ sn.disabled = false; } };
    card.append(sn);
    out.append(card);
  });
}
function renderNotes(){
  const out = $("#noteList"); out.replaceChildren();
  const items = cur.notes||[];
  if (!items.length){
    const e0 = el("div","empty"); e0.append(el("b",null,t("notes.empty.title")), el("span",null,t("notes.empty.body")));
    out.append(e0); return;
  }
  // Disclose the server-side notes cap (notes_omitted) rather than silently
  // dropping the oldest notes — same honesty rule as chat.earlier (v0.2.250).
  if (cur.notes_omitted)
    out.append(el("div","empty", t("notes.earlier").replace("{n}", cur.notes_omitted)));
  items.forEach(n=>{
    const card = el("div","card");
    const x = el("button","x","×"); x.setAttribute("aria-label",t("a11y.delnote"));
    x.onclick = async ()=>{ x.disabled = true; try{ await api(`/api/notes/${n.id}`,{method:"DELETE"}); openNotebook(cur.id);}catch(e){toast(e.message);} finally{ x.disabled = false; } };
    card.append(x, el("h4",null,n.title), el("div","bd",n.body));
    out.append(card);
  });
}
$("#noteForm").onsubmit = async e=>{
  e.preventDefault(); if(!cur) return;
  const nbId = cur.id;
  const btn = e.target.querySelector('button[type="submit"]');
  btn.disabled = true;
  try{
    await jpost(`/api/notebooks/${nbId}/notes`, {title:$("#noteTitle").value, body:$("#noteBody").value});
    $("#noteTitle").value=""; $("#noteBody").value=""; openNotebook(nbId);
  }catch(err){ toast(err.message); }
  finally{ btn.disabled = false; }
};
$("#reindexBtn").onclick = async ()=>{
  if (!cur) return;
  const btn = $("#reindexBtn");
  btn.disabled = true; const old = btn.textContent; btn.textContent = t("busy");
  try{
    const j = await (await api(`/api/notebooks/${cur.id}/reindex`, {method:"POST"})).json();
    toast(t("reindex.ok").replace("{n}", j.n_embedded).replace("{total}", j.n_total));
  }catch(err){ toast(err.message); }
  finally{ btn.disabled = false; btn.textContent = old; }
};

/* ---------- adders ---------- */
$("#nbForm").onsubmit = async e=>{
  e.preventDefault();
  const btn = e.target.querySelector('button[type="submit"]');
  btn.disabled = true;
  try{
    const j = await (await jpost("/api/notebooks", {name:$("#nbName").value})).json();
    $("#nbName").value=""; await openNotebook(j.id);
  }catch(err){ toast(err.message); }
  finally{ btn.disabled = false; }
};
$("#fileInput").onchange = async e=>{
  const f = e.target.files[0]; if(!f || !cur) return;
  const nbId = cur.id;
  e.target.disabled = true;
  try{
    const j = await (await api(`/api/notebooks/${nbId}/upload`, {method:"POST",
      headers:{"X-Filename":encodeURIComponent(f.name),"Content-Type":"application/octet-stream"}, body:f})).json();
    e.target.value=""; toast(t("sources.added") + (j.pages_failed ? " " + t("src.pages_failed").replace("{n}", j.pages_failed) : "") + embedNote(j)); openNotebook(nbId);
  }catch(err){ e.target.value=""; toast(err.message); }
  finally{ e.target.disabled = false; }
};
$("#urlBtn").onclick = async ()=>{
  const u = $("#urlInput").value.trim(); if(!u || !cur) return;
  const nbId = cur.id;
  const btn = $("#urlBtn");
  btn.disabled = true; const old = btn.textContent; btn.textContent = t("busy");
  try{ const j = await (await jpost(`/api/notebooks/${nbId}/sources`, {target:u})).json();
    $("#urlInput").value=""; toast(t("sources.added") + (j.pages_failed ? " " + t("src.pages_failed").replace("{n}", j.pages_failed) : "") + embedNote(j)); openNotebook(nbId);
  }catch(err){ toast(err.message); }
  finally{ btn.disabled = false; btn.textContent = old; }
};

/* ---------- tabs / lang / health ---------- */
const selectTab = b=>{
  document.querySelectorAll(".tabs button").forEach(x=>x.setAttribute("aria-selected","false"));
  b.setAttribute("aria-selected","true");
  document.querySelectorAll(".pane").forEach(p=>p.classList.remove("active"));
  $("#"+b.dataset.pane).classList.add("active");
};
document.querySelectorAll(".tabs button").forEach(b=>{
  b.onclick = ()=>selectTab(b);
  // WAI-ARIA tabs: arrows move focus+selection, Home/End jump to the ends.
  b.onkeydown = e=>{
    if (!["ArrowLeft","ArrowRight","Home","End"].includes(e.key)) return;
    e.preventDefault();
    const tabs=[...document.querySelectorAll(".tabs button")];
    const i=tabs.indexOf(b);
    const n = e.key==="Home" ? 0 : e.key==="End" ? tabs.length-1
      : (i + (e.key==="ArrowRight"?1:-1) + tabs.length) % tabs.length;
    tabs[n].focus(); selectTab(tabs[n]);
  };
});
$("#langBtn").onclick = ()=>{
  lang = lang==="ja" ? "en":"ja"; _lsSet("shoin.lang", lang);
  applyI18n(); buildKindButtons(); if (cur) renderNotebook();
};
/* Global shortcuts (v0.2.642): `/` focuses the question box, 1/2/3 select the
   pane at that position in the tab row — same discovery surface as the
   always-present tab strip on narrow viewports. Inert while typing in an
   editable element, so they never steal keys from an input. */
document.addEventListener("keydown", e=>{
  if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
  // the viewer owns the keys while open — a .focus() into the page behind
  // it would punch through its focus trap
  if ($("#viewer").classList.contains("open")) return;
  const el = e.target;
  const tag = el && el.tagName;
  if (tag==="INPUT" || tag==="TEXTAREA" || tag==="SELECT" ||
      (el && el.isContentEditable)) return;
  if (e.key==="/"){ e.preventDefault(); $("#askInput").focus(); return; }
  const i = {"1":0, "2":1, "3":2}[e.key];
  if (i==null) return;
  const tabs = [...document.querySelectorAll(".tabs button")];
  if (!tabs[i]) return;
  e.preventDefault(); tabs[i].focus(); selectTab(tabs[i]);
});
async function health(){
  try{
    const j = await (await api("/api/health")).json();
    const wasOn = !!window._llmOn;
    window._llmOn = !!j.llm;
    window._embedOn = !!j.embed_model;
    $("#lamp").classList.toggle("on", j.llm);
    $("#lampText").textContent = j.llm ? t("app.lamp.on") : t("app.lamp.off");
    const parts = [];
    if (j.model) parts.push(`LLM: ${j.model}`);
    if (j.embed_model) parts.push(`embed: ${j.embed_model}`);
    $("#lamp").title = parts.join(" / ");
    const b = $("#banner");
    b.style.display = j.llm ? "none" : "block";
    if (!j.llm) b.textContent = t("banner.offline");
    // Re-fetch questions when LLM goes from off→on (e.g. first health check
    // completes after notebook was already loaded, or LLM restarts).
    if (!wasOn && j.llm) refreshQuestions();
  }catch(_e){
    window._llmOn=false;
    window._embedOn=false;
    // Unreachable/invalid health response: the lamp must not keep claiming the
    // LLM is on while _llmOn says otherwise — show the same offline state the
    // !j.llm branch uses instead of leaving a stale green light.
    $("#lamp").classList.remove("on");
    $("#lampText").textContent = t("app.lamp.off");
    const b = $("#banner");
    b.style.display = "block";
    b.textContent = t("banner.offline");
  }
}

/* ---------- boot ---------- */
applyI18n(); buildKindButtons();
health(); setInterval(health, 20000);
loadNotebooks().catch(e=>toast(e.message));
