"""OpenAI-compatible client for local runtimes (Ollama / llama.cpp / LM Studio).

stdlib-only (urllib). Chat (blocking + SSE streaming) and embeddings. All
failures raise LLMError with stable codes so callers can degrade gracefully
(REQ-008): search keeps working when no LLM endpoint is reachable.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

from .config import embed_model, llm_model, llm_url

CHAT_TIMEOUT_SEC = 180
EMBED_TIMEOUT_SEC = 60
HEALTH_TIMEOUT_SEC = 3


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
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise LLMError(
                "SYSTEM_LLM_HTTP_ERROR", f"HTTP {exc.code} from {path}: {detail}"
            ) from exc
        except OSError as exc:
            raise LLMError(
                "SYSTEM_SERVICE_UNAVAILABLE",
                f"LLM endpoint unreachable at {self.base_url}: {exc}",
            ) from exc
        except json.JSONDecodeError as exc:
            raise LLMError("SYSTEM_LLM_BAD_RESPONSE", f"invalid JSON from {path}") from exc

    # --- capabilities ---

    def available(self) -> bool:
        """Cheap health check against /models."""
        req = urllib.request.Request(f"{self.base_url}/models")
        try:
            with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT_SEC):
                return True
        except OSError:
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
        try:
            with urllib.request.urlopen(req, timeout=CHAT_TIMEOUT_SEC) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        obj = json.loads(payload)
                        delta = obj["choices"][0]["delta"].get("content")
                    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                        continue
                    if delta:
                        yield str(delta)
        except urllib.error.HTTPError as exc:
            raise LLMError("SYSTEM_LLM_HTTP_ERROR", f"HTTP {exc.code} (stream)") from exc
        except OSError as exc:
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
            return [[float(x) for x in item["embedding"]] for item in items]
        except (KeyError, TypeError, ValueError) as exc:
            raise LLMError("SYSTEM_LLM_BAD_RESPONSE", "missing embeddings in response") from exc

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]
