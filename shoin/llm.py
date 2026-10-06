"""OpenAI-compatible client for local runtimes (Ollama / llama.cpp / LM Studio).

stdlib-only (urllib). Chat (blocking + SSE streaming) and embeddings. All
failures raise LLMError with stable codes so callers can degrade gracefully
(REQ-008): search keeps working when no LLM endpoint is reachable.
"""

from __future__ import annotations

import http.client
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

from .config import (
    embed_model,
    endpoint_is_external,
    llm_api_key,
    llm_model,
    llm_retries,
    llm_url,
    redact_url_credentials,
)

CHAT_TIMEOUT_SEC = 180

# Upper bound on generated tokens per request (v0.2.219).  Without it the
# only stop is the endpoint's own default — llama.cpp's n_predict=-1 and
# Ollama's num_predict=-1 both generate until context exhaustion, so the
# degeneration loops the citation report *detects* also *consume* the whole
# remaining context window (minutes of garbage on CPU-scale hardware).
# max_tokens is a core OpenAI field accepted by llama.cpp, Ollama, vLLM and
# llamafile alike.  4096 is deliberately generous — far above any legitimate
# answer or Studio output for a ~2400-token context budget — so the cap
# bounds runaway generation without shaping real output.
MAX_TOKENS = 4096
EMBED_TIMEOUT_SEC = 60
HEALTH_TIMEOUT_SEC = 3

# 32 MB — guard against a runaway/malicious endpoint. Shared by _post() (single
# resp.read() call) and chat_stream() (cumulative bytes across the SSE loop,
# v0.2.85 — chat_stream() had no cap at all despite handling the identical
# threat model _post() was fixed for in v0.2.37).
_MAX_RESPONSE = 32 * 1024 * 1024

# Retry budget for _post (v0.2.639): only transport-level codes — a refused
# connection or socket timeout from a local runtime that is restarting or
# still loading its model. HTTP_ERROR and BAD_RESPONSE are deterministic
# server answers; retrying them just multiplies the wait to the same result.
_RETRYABLE = frozenset({"SYSTEM_LLM_TIMEOUT", "SYSTEM_SERVICE_UNAVAILABLE"})
_RETRY_BACKOFF_SEC = 0.25


class LLMError(Exception):
    """LLM transport/protocol error with a stable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


Message = dict[str, str]


def _strip_surrogates(s: str) -> str:
    """Drop code points that cannot encode to UTF-8.

    json.loads materializes *lone* surrogates from \\ud800-style escapes a
    buggy endpoint or proxy can emit (valid pairs are already combined by
    the decoder). A lone surrogate reaching a sqlite bind or an
    ensure_ascii=False response encode escapes as a raw UnicodeEncodeError
    — and text cached or persisted first (questions_cache, messages,
    studio_outputs) re-crashes on every later read. Astral characters are
    unaffected: the JSON decoder pairs them before we ever see the str.
    """
    if s.isascii():
        return s
    return s.encode("utf-8", "ignore").decode("utf-8")


def _message_text(content: object) -> str:
    """Normalize an OpenAI `content` field to plain text.

    The schema allows a string OR an array of parts
    ([{"type": "text", "text": "..."}]) — some compatible servers and
    proxies pass the parts form through verbatim. str() on either shape
    would present Python-repr garbage ("[{'type': 'text', ...}]") as the
    answer text, badges and all.
    """
    if isinstance(content, str):
        return _strip_surrogates(content)
    if isinstance(content, list):
        return _strip_surrogates(
            "".join(
                part["text"]
                for part in content
                if isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            )
        )
    raise LLMError("SYSTEM_LLM_BAD_RESPONSE", "non-text content in LLM response")


class LLMClient:
    """Minimal OpenAI-compatible API client bound to one base URL."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        embedding_model: str | None = None,
    ) -> None:
        self.base_url = (base_url or llm_url()).rstrip("/")
        # v0.2.674 (product-review #58): the product promise is that
        # document text and questions never leave this machine — a
        # non-loopback endpoint silently breaks it. Warn once at client
        # construction (every real surface builds the client once per
        # process) instead of inside the per-request paths.
        if endpoint_is_external(self.base_url):
            print(
                "Warning: LLM endpoint is not local"
                f" ({redact_url_credentials(self.base_url)})"
                " — chunk text and questions"
                " leave this machine",
                file=sys.stderr,
            )
        self.model = model or llm_model()
        self.embedding_model = embedding_model if embedding_model is not None else embed_model()
        # finish_reason of the most recent chat/chat_stream call ("stop",
        # "length", …), or None when the endpoint omitted it or no call ran.
        # "length" means the answer stopped at MAX_TOKENS — callers surface it
        # as report.truncated instead of presenting a clipped answer as whole.
        self.last_finish_reason: str | None = None
        self.retries = llm_retries()
        # Auth gateways (vLLM behind a proxy, hosted OpenAI-compatible) need
        # a Bearer token; local runtimes ignore auth entirely. Attached only
        # when configured — never logged (error paths report codes/details,
        # not request headers).
        key = llm_api_key()
        self._headers = {"Content-Type": "application/json"}
        if key:
            self._headers["Authorization"] = f"Bearer {key}"

    # --- transport ---

    def _post(self, path: str, payload: dict[str, Any], timeout: int) -> Any:
        """_post_once + bounded retry on transport failures (v0.2.639).

        Only idempotent callers route here (chat, embed — both unobservable
        until return). chat_stream keeps its own no-retry path: deltas
        already emitted are visible output a retry would duplicate.
        available() is deliberately excluded too — the health probe exists
        to answer "is it up" fast, not to wait for it to come up.
        """
        attempt = 0
        while True:
            try:
                return self._post_once(path, payload, timeout)
            except LLMError as exc:
                if exc.code not in _RETRYABLE or attempt >= self.retries:
                    raise
                time.sleep(_RETRY_BACKOFF_SEC * (2**attempt))
                attempt += 1

    def _post_once(self, path: str, payload: dict[str, Any], timeout: int) -> Any:
        try:
            # Request() itself parses base_url via urlsplit — a malformed one
            # (unclosed IPv6 bracket) raises ValueError here, not in urlopen,
            # so construction stays inside the try.
            req = urllib.request.Request(
                f"{self.base_url}{path}",
                data=json.dumps(payload).encode("utf-8"),
                headers=self._headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                # Read one byte beyond the limit so len > _MAX_RESPONSE is the correct
                # truncation signal — len == _MAX_RESPONSE means the response fit exactly
                # (no truncation), which was wrongly rejected by the previous == check.
                raw = resp.read(_MAX_RESPONSE + 1)
                if len(raw) > _MAX_RESPONSE:
                    raise LLMError("SYSTEM_LLM_BAD_RESPONSE", "response exceeded 32 MB size limit")
                return json.loads(raw.decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            detail = exc.read(300).decode("utf-8", errors="replace")
            raise LLMError(
                "SYSTEM_LLM_HTTP_ERROR", f"HTTP {exc.code} from {path}: {detail}"
            ) from exc
        except (json.JSONDecodeError, RecursionError) as exc:
            # Must precede (OSError, ValueError): json.JSONDecodeError is a ValueError
            # subclass and would otherwise be misrouted to SYSTEM_SERVICE_UNAVAILABLE.
            # RecursionError is the same malformed-response signal: a deeply
            # nested body (an endpoint can trivially emit one) overflows the
            # decoder's recursion budget — it is not a service-availability
            # error and must not escape uncoded.
            raise LLMError("SYSTEM_LLM_BAD_RESPONSE", f"invalid JSON from {path}") from exc
        except (OSError, ValueError, http.client.HTTPException) as exc:
            # urllib wraps socket.timeout in URLError(reason=TimeoutError(...));
            # bare TimeoutError also has no .reason, so fall back to exc itself.
            # ValueError is raised for unknown URL schemes (e.g. SHOIN_LLM_URL=file://...).
            # http.client.HTTPException covers IncompleteRead (truncated response body)
            # and BadStatusLine (malformed HTTP status line from a non-HTTP server).
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                raise LLMError(
                    "SYSTEM_LLM_TIMEOUT",
                    f"LLM request timed out after {timeout}s",
                ) from exc
            raise LLMError(
                "SYSTEM_SERVICE_UNAVAILABLE",
                "LLM endpoint unreachable at"
                f" {redact_url_credentials(self.base_url)}: {exc}",
            ) from exc

    # --- capabilities ---

    def available(self) -> bool:
        """Cheap health check against /models."""
        try:
            # Request() construction itself can raise ValueError for a malformed
            # base_url (e.g. an unclosed IPv6 bracket, "http://[::1:11434/v1" —
            # a plausible typo) via urllib.parse.urlsplit(). This MUST be inside
            # the try: it was previously built before the try block began, so
            # this exact ValueError escaped uncaught, despite the except clause
            # below already listing "ValueError: unknown URL scheme" as a case
            # it exists to catch — ADDRESS/SCHEME parsing errors happen at
            # Request() construction time, not just at urlopen() time.
            req = urllib.request.Request(
                f"{self.base_url}/models", headers=self._headers
            )
            with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT_SEC) as resp:
                # Check Content-Type to distinguish LLM API servers (application/json)
                # from plain HTTP servers (text/html) that also return HTTP 200 on any
                # path.  Without this check, available() returned True for nginx/http.server,
                # causing every subsequent chat() to fail with SYSTEM_LLM_BAD_RESPONSE
                # instead of the graceful SYSTEM_SERVICE_UNAVAILABLE degradation path.
                ct = resp.getheader("Content-Type", "")
                return "json" in ct
        except (OSError, ValueError, http.client.HTTPException, AttributeError):
            # ValueError: unknown URL scheme.  HTTPException: BadStatusLine from a
            # non-HTTP server occupying the configured port.  AttributeError: urlopen
            # mock/stub without getheader() (also guards against unusual WSGI shims).
            return False

    # --- chat ---

    def chat(self, messages: list[Message], temperature: float = 0.2) -> str:
        self.last_finish_reason = None
        data = self._post(
            "/chat/completions",
            {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "stream": False,
                "max_tokens": MAX_TOKENS,
            },
            CHAT_TIMEOUT_SEC,
        )
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("SYSTEM_LLM_BAD_RESPONSE", "missing choices in response") from exc
        if content is None:
            raise LLMError("SYSTEM_LLM_BAD_RESPONSE", "null content in LLM response")
        content = _message_text(content)
        if isinstance(choice, dict) and isinstance(choice.get("finish_reason"), str):
            self.last_finish_reason = choice["finish_reason"]
        return content

    def chat_stream(self, messages: list[Message], temperature: float = 0.2) -> Iterator[str]:
        """Yield content deltas from an SSE streaming chat completion."""
        total_bytes = 0
        self.last_finish_reason = None
        try:
            # Request() parses base_url eagerly — an unclosed IPv6 bracket
            # raises ValueError here (inside the try), mapping to the same
            # SYSTEM_SERVICE_UNAVAILABLE the unreachable-endpoint path uses.
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=json.dumps(
                    {
                        "model": self.model,
                        "messages": messages,
                        "temperature": temperature,
                        "stream": True,
                        "max_tokens": MAX_TOKENS,
                    }
                ).encode("utf-8"),
                headers=self._headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=CHAT_TIMEOUT_SEC) as resp:
                for raw in resp:
                    total_bytes += len(raw)
                    if total_bytes > _MAX_RESPONSE:
                        raise LLMError(
                            "SYSTEM_LLM_BAD_RESPONSE", "stream exceeded 32 MB size limit"
                        )
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        obj = json.loads(payload)
                        if "error" in obj:
                            err = obj["error"]
                            msg = err if isinstance(err, str) else json.dumps(err)
                            raise LLMError(
                                "SYSTEM_LLM_BAD_RESPONSE",
                                f"LLM stream error: {str(msg)[:200]}",
                            )
                        # choices[0].finish_reason arrives on the final chunk
                        # (None on intermediate ones); keep the last one. A
                        # finish chunk may omit "delta" entirely, so capture
                        # before the delta read — the truncation signal must
                        # not hinge on an unrelated field being present.
                        choice = obj["choices"][0]
                        if isinstance(choice, dict) and isinstance(
                            choice.get("finish_reason"), str
                        ):
                            self.last_finish_reason = choice["finish_reason"]
                        raw_delta = choice["delta"]
                        if isinstance(raw_delta, dict):
                            delta = raw_delta.get("content")
                        elif isinstance(raw_delta, str):
                            # Compatible servers may emit the delta as a bare
                            # string instead of the OpenAI object shape.
                            delta = raw_delta
                        else:
                            delta = None
                        if isinstance(delta, list):
                            delta = _message_text(delta)
                    except LLMError:
                        raise
                    except (
                        json.JSONDecodeError,
                        KeyError,
                        IndexError,
                        TypeError,
                        RecursionError,
                    ):
                        # RecursionError too: a deeply nested frame is a
                        # malformed delta — drop it like every other parse
                        # failure rather than letting it abort the stream.
                        continue
                    # A malformed non-text delta is dropped rather than
                    # str()-coerced — repr garbage mid-stream would land in the
                    # persisted answer text (same shape as chat()'s fix above).
                    if isinstance(delta, str):
                        delta = _strip_surrogates(delta)
                        if delta:
                            yield delta
        except urllib.error.HTTPError as exc:
            raise LLMError("SYSTEM_LLM_HTTP_ERROR", f"HTTP {exc.code} (stream)") from exc
        except (OSError, ValueError, http.client.HTTPException) as exc:
            # http.client.HTTPException covers IncompleteRead raised when the TCP
            # connection is dropped before the SSE stream sends data: [DONE].
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                raise LLMError(
                    "SYSTEM_LLM_TIMEOUT",
                    f"LLM stream timed out after {CHAT_TIMEOUT_SEC}s",
                ) from exc
            raise LLMError(
                "SYSTEM_SERVICE_UNAVAILABLE",
                "LLM endpoint unreachable at"
                f" {redact_url_credentials(self.base_url)}: {exc}",
            ) from exc

    # --- embeddings ---

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed *texts*. Raises LLMError if no embedding model is configured."""
        if not (self.embedding_model or "").strip():
            raise LLMError("SYSTEM_EMBED_DISABLED", "no embedding model configured")
        data = self._post(
            "/embeddings",
            {"model": self.embedding_model, "input": texts},
            EMBED_TIMEOUT_SEC,
        )
        try:
            items = sorted(data["data"], key=lambda d: int(d.get("index", 0)))
            vecs = [[float(x) for x in item["embedding"]] for item in items]
        except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
            raise LLMError("SYSTEM_LLM_BAD_RESPONSE", "missing embeddings in response") from exc
        if len(vecs) != len(texts):
            raise LLMError(
                "SYSTEM_LLM_BAD_RESPONSE",
                f"embedding count mismatch: got {len(vecs)}, expected {len(texts)}",
            )
        dims = {len(v) for v in vecs}
        if len(dims) > 1:
            raise LLMError(
                "SYSTEM_LLM_BAD_RESPONSE",
                f"inconsistent embedding dimensions in response: {dims}",
            )
        return vecs

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]
