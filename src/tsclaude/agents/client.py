"""Client protocol, error types and the scripted mock client.

`AgentClient.complete` is synchronous; the gate enforces the per-call deadline itself (a client
that ignores its timeout cannot hold a signal). Tests and the default path only ever use
`MockClient`; the real Anthropic adapter is a stub (`real_client`).
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class ClientError(Exception):
    """Base class: any client failure (never an approval)."""


class ClientTimeout(ClientError):
    """The call did not complete within its deadline (may still be billed)."""


class ClientUnavailable(ClientError):
    """The API refused or could not be reached before generating (assumed not billed)."""


@dataclass(frozen=True)
class CompletionRequest:
    role: str
    model: str
    system: str
    user: str
    max_output_tokens: int
    timeout_s: float


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0


@dataclass(frozen=True)
class CompletionResponse:
    text: str
    usage: Usage
    model: str


class AgentClient(Protocol):
    def complete(self, request: CompletionRequest) -> CompletionResponse: ...


# ---------------------------------------------------------------------- mock


class StepKind(StrEnum):
    TEXT = "text"  # return `text` (valid or not: the validator decides)
    TIMEOUT = "timeout"  # raise ClientTimeout
    API_DOWN = "api_down"  # raise ClientUnavailable
    RAISE = "raise"  # raise an unexpected exception (a client bug)
    SLOW = "slow"  # sleep `delay_s`, then return `text` (exercises the gate's deadline)


@dataclass(frozen=True)
class MockStep:
    kind: StepKind
    text: str = ""
    delay_s: float = 0.0
    usage: Usage | None = None

    @classmethod
    def valid(cls, payload: dict[str, object], usage: Usage | None = None) -> MockStep:
        return cls(StepKind.TEXT, json.dumps(payload), usage=usage)

    @classmethod
    def raw(cls, text: str, usage: Usage | None = None) -> MockStep:
        return cls(StepKind.TEXT, text, usage=usage)

    @classmethod
    def invalid_json(cls) -> MockStep:
        return cls(StepKind.TEXT, "Sure! Here is my assessment: {not json")

    @classmethod
    def timeout(cls) -> MockStep:
        return cls(StepKind.TIMEOUT)

    @classmethod
    def api_down(cls) -> MockStep:
        return cls(StepKind.API_DOWN)

    @classmethod
    def crash(cls) -> MockStep:
        return cls(StepKind.RAISE)

    @classmethod
    def slow(cls, delay_s: float, payload: dict[str, object]) -> MockStep:
        return cls(StepKind.SLOW, json.dumps(payload), delay_s=delay_s)


def estimate_tokens(text: str) -> int:
    """Rough token count (about 4 characters per token). Not a tokenizer: labeled an estimate."""
    return max(1, (len(text) + 3) // 4)


@dataclass
class MockClient:
    """Deterministic scripted client. Steps are consumed per role (`analyst`, `skeptic`) in order;
    a role with no script left raises `ClientUnavailable` (so an unexpected extra call fails the
    test loudly instead of approving). Every request is recorded in `calls`."""

    scripts: dict[str, deque[MockStep]] = field(default_factory=dict)
    calls: list[CompletionRequest] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def scripted(
        cls, analyst: Iterable[MockStep] = (), skeptic: Iterable[MockStep] = ()
    ) -> MockClient:
        return cls(scripts={"analyst": deque(analyst), "skeptic": deque(skeptic)})

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        with self._lock:
            self.calls.append(request)
            queue = self.scripts.get(request.role)
            step = queue.popleft() if queue else None
        if step is None:
            raise ClientUnavailable("mock: no scripted response left")
        if step.kind is StepKind.TIMEOUT:
            raise ClientTimeout("mock: timeout")
        if step.kind is StepKind.API_DOWN:
            raise ClientUnavailable("mock: API down")
        if step.kind is StepKind.RAISE:
            raise RuntimeError("mock: unexpected client bug")
        if step.kind is StepKind.SLOW:
            time.sleep(step.delay_s)
        usage = step.usage or Usage(
            input_tokens=estimate_tokens(request.system + request.user),
            output_tokens=estimate_tokens(step.text),
        )
        return CompletionResponse(text=step.text, usage=usage, model=request.model)
