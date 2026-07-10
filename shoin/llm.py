"""OpenAI-compatible client for local runtimes (Ollama / llama.cpp / LM Studio).

stdlib-only (urllib). Chat (blocking + SSE streaming) and embeddings. All
failures raise LLMError with stable codes so callers can degrade gracefully
(REQ-008): search keeps working when no LLM endpoint is reachable.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

from .config import embed_model, llm_model, llm_url

CHAT_TIMEOUT_SEC = 180
EMBED_TIMEOUT_SEC = 60
HEALTH_TIMEOUT_SEC = 3

# 32 MB — guard against a runaway/malicious endpoint. Shared by _post() (single
# resp.read() call) and chat_stream() (cumulative bytes across the SSE loop,
# v0.2.85 — chat_stream() had no cap at all despite handling the identical
# threat model _post() was fixed for in v0.2.37).
_MAX_RESPONSE = 32 * 1024 * 1024


class LLMError(Exception):
    """LLM transport/protocol error with a stable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


Message = dict[str, str]


class LLMClient:
    """Minimal OpenAI-compatible API client bound to one base URL."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        embedding_model: str | None = None,
    ) -> None:
        self.base_url = (base_url or llm_url()).rstrip("/")
        self.model = model or llm_model()
        self.embedding_model = embedding_model if embedding_model is not None else embed_model()

    # --- transport ---

    def _post(self, path: str, payload: dict[str, Any], timeout: int) -> Any:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
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
        except json.JSONDecodeError as exc:
            # Must precede (OSError, ValueError): json.JSONDecodeError is a ValueError
            # subclass and would otherwise be misrouted to SYSTEM_SERVICE_UNAVAILABLE.
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
                f"LLM endpoint unreachable at {self.base_url}: {exc}",
            ) from exc

    # --- capabilities ---

    def available(self) -> bool:
        """Cheap health check against /models."""
        req = urllib.request.Request(f"{self.base_url}/models")
        try:
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
        data = self._post(
            "/chat/completions",
            {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "stream": False,
            },
            CHAT_TIMEOUT_SEC,
        )
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("SYSTEM_LLM_BAD_RESPONSE", "missing choices in response") from exc
        if content is None:
            raise LLMError("SYSTEM_LLM_BAD_RESPONSE", "null content in LLM response")
        return str(content)

    def chat_stream(self, messages: list[Message], temperature: float = 0.2) -> Iterator[str]:
        """Yield content deltas from an SSE streaming chat completion."""
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(
                {
                    "model": self.model,
                    "messages": messages,
                    "temperature": temperature,
                    "stream": True,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        total_bytes = 0
        try:
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
                        delta = obj["choices"][0]["delta"].get("content")
                    except LLMError:
                        raise
                    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                        continue
                    if delta:
                        yield str(delta)
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
                f"LLM endpoint unreachable at {self.base_url}: {exc}",
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
