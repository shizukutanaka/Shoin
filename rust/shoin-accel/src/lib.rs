//! Optional Rust kernels for Shoin's compute-bound paths.
//!
//! This module is an *accelerator only*: `shoin/accel.py` imports it when
//! present and falls back to the pure-Python implementations whenever it is
//! missing, disabled (`SHOIN_ACCEL=0`), or raises. Every function here is a
//! bit-exact re-implementation of the Python semantics it accelerates —
//! ranking order and scores must not change:
//!
//! * `vector_topk` mirrors `shoin.search.vector_search`'s scan: embeddings
//!   are little/native-endian f32 blobs; dot products and norms accumulate
//!   in f64 with Neumaier compensation, operation-for-operation identical
//!   to CPython 3.12's built-in `sum()` over floats, so scores are
//!   bit-identical for finite inputs (the only kind the store admits —
//!   non-finite embeddings are rejected at ingest). Degenerate rows keep
//!   the Python degradations: odd-length blob → skipped, stored norm used
//!   as-is (NULL → computed), dimension mismatch / zero norm / non-finite
//!   or non-positive score → excluded.
//! * `needle_pool` mirrors the candidate selection of the SQL LIKE fallback
//!   in `shoin.search.bm25_search`: SQLite's `LOWER()` and `LIKE` fold
//!   ASCII case only, and the SQL score counts non-overlapping occurrences
//!   (via `REPLACE` length arithmetic) plus a flat context bonus per needle
//!   present in the context. The returned pool (score DESC, id ASC, capped)
//!   is exactly the row set/order the CTE produced; final scoring stays in
//!   Python (`_needle_score`, with its Unicode `casefold` semantics).
//! * `estimate_tokens` / `tail` mirror `shoin.chunk`'s token estimator and
//!   tail cutter, including the CJK range table (passed in from Python as
//!   packed little-endian u32 bounds so the table has a single owner) and
//!   Python's `str.lstrip()` whitespace set (Unicode White_Space plus the
//!   ASCII information separators U+001C–U+001F, which Python also treats
//!   as whitespace).

use std::cmp::Ordering;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

/// Neumaier (compensated) summation — the same branch structure CPython
/// 3.12's `sum()` uses for floats, so results are bit-identical.
fn neumaier_sum(values: impl Iterator<Item = f64>) -> f64 {
    let mut sum = 0.0_f64;
    let mut c = 0.0_f64;
    for x in values {
        let t = sum + x;
        if sum.abs() >= x.abs() {
            c += (sum - t) + x;
        } else {
            c += (x - t) + sum;
        }
        sum = t;
    }
    sum + c
}

/// Decode one native-endian f32 from a blob (the store writes
/// `array("f", vec).tobytes()`, i.e. platform-native f32).
fn blob_f32(blob: &[u8], i: usize) -> f64 {
    let b = [
        blob[i * 4],
        blob[i * 4 + 1],
        blob[i * 4 + 2],
        blob[i * 4 + 3],
    ];
    f32::from_ne_bytes(b) as f64
}

/// Rank embedding rows by cosine similarity against `query`, keeping the
/// top `k` strictly-positive scores in fetch order on ties.
///
/// `rows` yields `(chunk_id, embedding_blob, stored_norm_or_None)`.
#[pyfunction]
fn vector_topk(
    query: Vec<f64>,
    rows: &Bound<'_, PyAny>,
    k: usize,
) -> PyResult<Vec<(i64, f64)>> {
    if k == 0 {
        return Ok(Vec::new());
    }
    let qn = neumaier_sum(query.iter().map(|v| v * v)).sqrt();
    if qn == 0.0 {
        // Python: every score degrades to 0.0 and only score > 0 is kept.
        // (A NaN query norm is *not* caught here: it propagates into NaN
        // scores below, which the finiteness filter drops — same result.)
        return Ok(Vec::new());
    }
    let mut scored: Vec<(f64, i64)> = Vec::new();
    for item in rows.iter()? {
        let item = item?;
        let id: i64 = item.get_item(0)?.extract()?;
        let blob: Bound<'_, PyBytes> = item.get_item(1)?.extract()?;
        let bytes: &[u8] = blob.as_bytes();
        if bytes.len() % 4 != 0 {
            continue; // undecodable blob: the Python scan skips the row
        }
        let dim = bytes.len() / 4;
        if dim != query.len() {
            continue; // dimension mismatch scores 0.0 in Python → excluded
        }
        let stored: Option<f64> = item.get_item(2)?.extract()?;
        let vn = match stored {
            Some(v) => v,
            None => neumaier_sum((0..dim).map(|i| {
                let v = blob_f32(bytes, i);
                v * v
            }))
            .sqrt(),
        };
        if vn == 0.0 {
            continue; // zero norm scores 0.0 in Python → excluded
        }
        let dot = neumaier_sum((0..dim).map(|i| query[i] * blob_f32(bytes, i)));
        let score = dot / (qn * vn);
        if score.is_finite() && score > 0.0 {
            scored.push((score, id));
        }
    }
    // Stable sort: ties keep fetch order, matching heapq.nlargest's
    // documented equivalence with sorted(..., reverse=True)[:k].
    scored.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(Ordering::Equal));
    scored.truncate(k);
    Ok(scored.into_iter().map(|(s, id)| (id, s)).collect())
}

/// ASCII-only lowercase, byte-wise — SQLite `LOWER()` semantics (SQLite
/// folds only A–Z; the needle side was lowered the same way by the SQL).
fn ascii_lower(bytes: &mut [u8]) {
    for b in bytes.iter_mut() {
        *b = b.to_ascii_lowercase();
    }
}

/// Aho-Corasick automaton over the needle set (built once per
/// `needle_pool` call; the needle count is small).
///
/// The SQL selector this replaces gets its speed from a cheap existence
/// filter (the LIKE/instr guard) before any counting; a naive per-needle
/// substring scan in Rust measured *slower* than the SQL at ~70 needles
/// (era-bridged queries) because it rescans the text once per needle.
/// One AC pass finds every needle's occurrences in O(text length),
/// which is what makes the kernel win at every needle count.
struct AcAutomaton {
    /// Trie transitions; -1 = no edge (follow failure links).
    next: Vec<[i32; 256]>,
    /// Failure link per state.
    fail: Vec<usize>,
    /// Pattern ids ending at each state (failure-link outputs merged
    /// in BFS order, so a state's list is complete before use).
    out: Vec<Vec<u32>>,
    /// Pattern lengths by id (for start-position reconstruction).
    lens: Vec<usize>,
}

impl AcAutomaton {
    fn build(patterns: &[Vec<u8>]) -> AcAutomaton {
        let mut next: Vec<[i32; 256]> = vec![[-1; 256]];
        let mut out: Vec<Vec<u32>> = vec![Vec::new()];
        for (pid, pat) in patterns.iter().enumerate() {
            let mut state = 0usize;
            for &b in pat {
                if next[state][b as usize] < 0 {
                    next[state][b as usize] = next.len() as i32;
                    next.push([-1; 256]);
                    out.push(Vec::new());
                }
                state = next[state][b as usize] as usize;
            }
            out[state].push(pid as u32);
        }
        // BFS over the trie: set failure links and merge each state's
        // output list with its failure target's (already final, since
        // the target sits at a strictly smaller depth and is therefore
        // discovered earlier in BFS order).
        let mut fail = vec![0usize; next.len()];
        let mut queue: std::collections::VecDeque<usize> = std::collections::VecDeque::new();
        for b in 0..256usize {
            let child = next[0][b];
            if child >= 0 {
                queue.push_back(child as usize);
            }
        }
        while let Some(state) = queue.pop_front() {
            for b in 0..256usize {
                let child = next[state][b as usize];
                if child < 0 {
                    continue;
                }
                let child = child as usize;
                let mut f = fail[state];
                while f != 0 && next[f][b as usize] < 0 {
                    f = fail[f];
                }
                let via = next[f][b as usize];
                fail[child] = if via >= 0 { via as usize } else { 0 };
                let inherited = out[fail[child]].clone();
                out[child].extend(inherited);
                queue.push_back(child);
            }
        }
        AcAutomaton {
            next,
            fail,
            out,
            lens: patterns.iter().map(|p| p.len()).collect(),
        }
    }

    /// One pass over `text`; `on_match(pattern_id, end_exclusive)` fires
    /// for every occurrence of every pattern, in end-position order
    /// (for a fixed pattern, therefore also in start-position order).
    fn scan(&self, text: &[u8], mut on_match: impl FnMut(u32, usize)) {
        let mut state = 0usize;
        for (i, &b) in text.iter().enumerate() {
            while state != 0 && self.next[state][b as usize] < 0 {
                state = self.fail[state];
            }
            let child = self.next[state][b as usize];
            state = if child >= 0 { child as usize } else { 0 };
            for &pid in &self.out[state] {
                on_match(pid, i + 1);
            }
        }
    }
}

/// Select the LIKE-fallback candidate pool: rows whose ASCII-folded text
/// contains a needle (counted per occurrence) or whose folded context
/// contains it (flat `ctx_weight` per needle), ordered score DESC, id ASC,
/// truncated to `cap`. Returns chunk ids in pool order.
///
/// `rows` yields `(chunk_id, text, context)`; `needles` are the raw
/// fallback needles (lowered here, ASCII-only, like the SQL's `LOWER(?)`).
#[pyfunction]
fn needle_pool(
    rows: &Bound<'_, PyAny>,
    needles: Vec<String>,
    cap: usize,
    ctx_weight: f64,
) -> PyResult<Vec<i64>> {
    let mut lowered: Vec<Vec<u8>> = Vec::with_capacity(needles.len());
    for n in needles {
        let mut b = n.into_bytes();
        if b.is_empty() {
            // The Python needle builder never emits empty needles; refuse
            // rather than guess (the caller falls back to the SQL path).
            return Err(PyValueError::new_err("empty needle"));
        }
        ascii_lower(&mut b);
        lowered.push(b);
    }
    let ac = AcAutomaton::build(&lowered);
    let n_needles = lowered.len();
    let mut scored: Vec<(f64, i64)> = Vec::new();
    for item in rows.iter()? {
        let item = item?;
        let id: i64 = item.get_item(0)?.extract()?;
        let text: String = item.get_item(1)?.extract()?;
        let context: String = item.get_item(2)?.extract()?;
        let mut lt = text.into_bytes();
        ascii_lower(&mut lt);
        let mut lc = context.into_bytes();
        ascii_lower(&mut lc);
        // Greedy non-overlapping count per needle — the count SQLite's
        // REPLACE-length arithmetic (and Python's str.count) computes.
        // AC reports a pattern's occurrences in start order, so the
        // greedy left-to-right selection is a single pass: accept an
        // occurrence only if it starts at or after the last accepted
        // occurrence's end.
        let mut counts = vec![0usize; n_needles];
        let mut last_end = vec![0usize; n_needles];
        ac.scan(&lt, |pid, end| {
            let p = pid as usize;
            let start = end - ac.lens[p];
            if start >= last_end[p] {
                counts[p] += 1;
                last_end[p] = end;
            }
        });
        let mut present = vec![false; n_needles];
        ac.scan(&lc, |pid, _end| {
            present[pid as usize] = true;
        });
        let mut score = 0.0_f64;
        for p in 0..n_needles {
            score += counts[p] as f64;
            if present[p] {
                score += ctx_weight;
            }
        }
        if score > 0.0 {
            scored.push((score, id));
        }
    }
    scored.sort_by(|a, b| {
        b.0.partial_cmp(&a.0)
            .unwrap_or(Ordering::Equal)
            .then_with(|| a.1.cmp(&b.1))
    });
    scored.truncate(cap);
    Ok(scored.into_iter().map(|(_, id)| id).collect())
}

/// Parse the packed CJK bounds table (little-endian u32 pairs
/// [lo, hi+1, lo, hi+1, ...], as packed by `shoin.chunk`).
fn parse_bounds(bytes: &[u8]) -> PyResult<Vec<u32>> {
    if bytes.len() % 4 != 0 {
        return Err(PyValueError::new_err("bounds length not a multiple of 4"));
    }
    Ok(bytes
        .chunks_exact(4)
        .map(|c| u32::from_le_bytes([c[0], c[1], c[2], c[3]]))
        .collect())
}

/// Membership in the merged CJK ranges: bisect_right parity, identical to
/// `shoin.chunk.is_cjk`.
fn is_cjk(bounds: &[u32], cp: u32) -> bool {
    bounds.partition_point(|&b| b <= cp) % 2 == 1
}

fn is_word_char(c: char) -> bool {
    matches!(c, 'A'..='Z' | 'a'..='z' | '0'..='9' | '_')
}

/// `_run_token_cost` in `shoin.chunk`: 1 up to 40 chars, then +1 per 4.
fn run_token_cost(n: i64) -> i64 {
    if n <= 40 {
        1
    } else {
        1 + (n - 40 + 3) / 4
    }
}

/// `shoin.chunk.estimate_tokens`: one token per CJK character plus the
/// run cost of every maximal [A-Za-z0-9_] run.
#[pyfunction]
fn estimate_tokens(text: &str, bounds: &Bound<'_, PyBytes>) -> PyResult<i64> {
    let bounds = parse_bounds(bounds.as_bytes())?;
    let mut total: i64 = 0;
    let mut run: i64 = 0;
    for ch in text.chars() {
        if is_word_char(ch) {
            run += 1;
            continue;
        }
        if run > 0 {
            total += run_token_cost(run);
            run = 0;
        }
        if is_cjk(&bounds, ch as u32) {
            total += 1;
        }
    }
    if run > 0 {
        total += run_token_cost(run);
    }
    Ok(total)
}

/// Python `str.isspace()` for one char: Unicode White_Space plus
/// U+001C–U+001F (which Python, unlike the Unicode property, includes).
fn py_isspace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

fn py_lstrip(s: &str) -> String {
    s.trim_start_matches(py_isspace).to_string()
}

/// `shoin.chunk._tail`: the suffix of `text` covering roughly `tokens`
/// tokens, scanned from the end with the same credit rules (CJK chars cost
/// 1 each; a word run costs its base token when it ends, with long runs
/// credited incrementally past 40 chars). Early exits return the suffix
/// `lstrip()`ed; a full scan returns the text untouched — as in Python.
#[pyfunction]
fn tail(text: &str, tokens: i64, bounds: &Bound<'_, PyBytes>) -> PyResult<String> {
    let bounds = parse_bounds(bounds.as_bytes())?;
    if tokens <= 0 {
        return Ok(String::new());
    }
    let mut acc: i64 = 0;
    let mut run_len: i64 = 0;
    let mut run_credited = false;
    for (byte_idx, ch) in text.char_indices().rev() {
        if is_cjk(&bounds, ch as u32) {
            if run_len > 0 && !run_credited {
                acc += 1;
                if acc >= tokens {
                    return Ok(py_lstrip(&text[byte_idx + ch.len_utf8()..]));
                }
            }
            run_len = 0;
            run_credited = false;
            acc += 1;
            if acc >= tokens {
                return Ok(py_lstrip(&text[byte_idx..]));
            }
        } else if is_word_char(ch) {
            run_len += 1;
            if run_len == 41 {
                run_credited = true;
                acc += 2;
                if acc >= tokens {
                    return Ok(py_lstrip(&text[byte_idx..]));
                }
            } else if run_len > 40 && (run_len - 40) % 4 == 1 {
                acc += 1;
                if acc >= tokens {
                    return Ok(py_lstrip(&text[byte_idx..]));
                }
            }
        } else {
            if run_len > 0 && !run_credited {
                acc += 1;
                if acc >= tokens {
                    return Ok(py_lstrip(&text[byte_idx + ch.len_utf8()..]));
                }
            }
            run_len = 0;
            run_credited = false;
        }
    }
    Ok(text.to_string())
}

#[pymodule]
fn _shoin_accel(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(vector_topk, m)?)?;
    m.add_function(wrap_pyfunction!(needle_pool, m)?)?;
    m.add_function(wrap_pyfunction!(estimate_tokens, m)?)?;
    m.add_function(wrap_pyfunction!(tail, m)?)?;
    m.add("__version__", "0.1.0")?;
    Ok(())
}
