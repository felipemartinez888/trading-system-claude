"""Real Anthropic client: **stub, not implemented** (the live half of the M5 gate is open).

- `api_key_present` only answers *whether* a key exists (environment variable or ``.env``); the
  value is never returned, stored, logged or put in any record (CLAUDE.md rule 8).
- `build_client` returns ``None`` for mode ``off`` without looking for a key at all, and ``None``
  when no key exists. Otherwise it returns `AnthropicClientStub`, whose every call raises
  `ClientUnavailable`, so the gate degrades to ``AI_UNAVAILABLE`` (never to an approval).
- The ``anthropic`` SDK is not a dependency and is not imported anywhere. Replacing the stub needs:
  the SDK as an optional dependency, imported lazily inside the adapter; ``messages.create`` with
  ``system`` / ``max_tokens`` / ``timeout``; usage mapped from ``input_tokens``,
  ``output_tokens``, ``cache_read_input_tokens``; transport errors mapped to `ClientUnavailable`
  and timeouts to `ClientTimeout`; a live measurement run on the owner's machine.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from .client import AgentClient, ClientUnavailable, CompletionRequest, CompletionResponse
from .config import AgentMode, AgentsConfig

KEY_NAME = "ANTHROPIC_API_KEY"


def _dotenv_has_key(path: Path) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        name, sep, value = line.strip().removeprefix("export ").partition("=")
        if sep and name.strip() == KEY_NAME and value.strip().strip("'\""):
            return True
    return False


def api_key_present(env: Mapping[str, str] | None = None, dotenv: Path | None = None) -> bool:
    source = os.environ if env is None else env
    if source.get(KEY_NAME, "").strip():
        return True
    return dotenv is not None and _dotenv_has_key(dotenv)


class AnthropicClientStub:
    """Placeholder for the real adapter. Every call fails as 'unavailable'."""

    def __repr__(self) -> str:  # never shows configuration or secrets
        return "AnthropicClientStub()"

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        raise ClientUnavailable("real Anthropic adapter not implemented (M5 live part open)")


def build_client(
    config: AgentsConfig, env: Mapping[str, str] | None = None, dotenv: Path | None = None
) -> AgentClient | None:
    if config.mode is AgentMode.OFF:
        return None
    if not api_key_present(env, dotenv):
        return None
    return AnthropicClientStub()
