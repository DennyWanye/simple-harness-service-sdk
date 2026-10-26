"""Explicit, content-free identity and replay state for logical Realtime sessions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .contracts import RealtimeError, RealtimeErrorCode


def _identity(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError("invalid recovery identity")
    return value


@dataclass(frozen=True, slots=True)
class RealtimeItemIdentity:
    canonical_id: str
    role: Literal["user", "assistant", "system"]
    source: str
    generation: int
    confirmed: bool
    aliases: tuple[str, ...] = ()


class RealtimeIdentityTable:
    def __init__(self) -> None:
        self.generation = 0
        self._items: dict[str, RealtimeItemIdentity] = {}

    def begin_generation(self, generation: int) -> None:
        self.generation = generation
        self._items = {
            alias: item
            for alias, item in self._items.items()
            if item.role == "user" and item.confirmed
        }

    def lookup(self, item_id: str) -> RealtimeItemIdentity | None:
        return self._items.get(item_id)

    def is_user(self, item_id: str) -> bool:
        item = self.lookup(item_id)
        return item is not None and item.role == "user" and item_id == item.canonical_id

    def register(
        self,
        item_id: str,
        *,
        role: Literal["user", "assistant", "system"],
        source: str,
        confirmed: bool,
        aliases: tuple[str, ...] = (),
    ) -> RealtimeItemIdentity:
        _identity(item_id)
        existing = self.lookup(item_id)
        if existing is not None and existing.role != role and role != "assistant":
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "conflicting item role")
        linked = tuple(dict.fromkeys((item_id, *aliases, *(existing.aliases if existing else ()))))
        for alias in linked:
            _identity(alias)
            previous = self.lookup(alias)
            if previous is not None and previous.canonical_id != item_id:
                raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "conflicting item alias")
        item = RealtimeItemIdentity(
            item_id,
            role,
            source,
            self.generation,
            confirmed or (existing.confirmed if existing and existing.role == role else False),
            linked,
        )
        for alias in linked:
            self._items[alias] = item
        return item

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            asdict(item)
            for key, item in sorted(self._items.items())
            if key == item.canonical_id and item.role == "user" and item.confirmed
        ]


@dataclass
class _SessionState:
    table: RealtimeIdentityTable = field(default_factory=RealtimeIdentityTable)
    tool_calls: set[str] = field(default_factory=set)
    completed_inputs: set[str] = field(default_factory=set)
    messages: dict[str, dict[str, str | None]] = field(default_factory=dict)


class RealtimeRecoveryState:
    """Caller-owned logical-session state; never stores text, credentials or audio."""

    def __init__(self) -> None:
        self._sessions: dict[str, _SessionState] = {}

    def _session(self, external_session_id: str) -> _SessionState:
        return self._sessions.setdefault(_identity(external_session_id), _SessionState())

    def identity_table(self, external_session_id: str) -> RealtimeIdentityTable:
        return self._session(external_session_id).table

    def begin_session(self, external_session_id: str, minimum_generation: int) -> int:
        table = self.identity_table(external_session_id)
        generation = max(table.generation + 1, minimum_generation)
        table.begin_generation(generation)
        return generation

    def tool_calls(self, external_session_id: str) -> set[str]:
        return self._session(external_session_id).tool_calls

    def completed_inputs(self, external_session_id: str) -> set[str]:
        return self._session(external_session_id).completed_inputs

    def claim_message(self, external_session_id: str, key: str, digest: str) -> str | None:
        messages = self._session(external_session_id).messages
        existing = messages.get(_identity(key))
        if existing is not None:
            if existing["digest"] != digest:
                raise RealtimeError(
                    RealtimeErrorCode.INVALID_REQUEST, "idempotency payload changed"
                )
            if existing["status"] != "confirmed":
                raise RealtimeError(RealtimeErrorCode.BUSY, "message outcome is ambiguous")
            return existing["item_id"]
        messages[key] = {"digest": digest, "status": "pending", "item_id": None}
        return None

    def confirm_message(self, external_session_id: str, key: str, item_id: str) -> None:
        entry = self._session(external_session_id).messages[key]
        entry.update(status="confirmed", item_id=item_id)

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "sessions": {
                key: {
                    "generation": state.table.generation,
                    "identities": state.table.snapshot(),
                    "tool_calls": sorted(state.tool_calls),
                    "completed_inputs": sorted(state.completed_inputs),
                    "messages": {k: dict(v) for k, v in state.messages.items()},
                }
                for key, state in self._sessions.items()
            },
        }

    @classmethod
    def from_snapshot(cls, value: object) -> RealtimeRecoveryState:
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "sessions"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
        ):
            raise ValueError("invalid recovery snapshot")
        sessions = value["sessions"]
        if not isinstance(sessions, dict) or len(sessions) > 1024:
            raise ValueError("invalid recovery sessions")
        result = cls()
        for key, record in sessions.items():
            if not isinstance(record, dict) or set(record) != {
                "generation",
                "identities",
                "tool_calls",
                "completed_inputs",
                "messages",
            }:
                raise ValueError("invalid recovery session")
            generation = record["generation"]
            if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
                raise ValueError("invalid recovery generation")
            state = result._session(_identity(key))
            state.table.generation = generation
            if (
                not isinstance(record["identities"], list)
                or not isinstance(record["tool_calls"], list)
                or not isinstance(record["completed_inputs"], list)
                or not isinstance(record["messages"], dict)
            ):
                raise ValueError("invalid recovery collections")
            if any(
                len(record[name]) > 10_000
                for name in ("identities", "tool_calls", "completed_inputs", "messages")
            ):
                raise ValueError("recovery collection is too large")
            for item in record["identities"]:
                if (
                    not isinstance(item, dict)
                    or set(item)
                    != {"canonical_id", "role", "source", "generation", "confirmed", "aliases"}
                    or item["role"] != "user"
                    or item["confirmed"] is not True
                ):
                    raise ValueError("invalid recovered user")
                if (
                    not isinstance(item["generation"], int)
                    or isinstance(item["generation"], bool)
                    or not 0 < item["generation"] <= generation
                ):
                    raise ValueError("invalid recovered item generation")
                if (
                    not isinstance(item["source"], str)
                    or item["source"]
                    not in {
                        "context_ack",
                        "speech_started",
                        "committed",
                        "transcript",
                        "output_item",
                        "input_audio_item",
                    }
                    or not isinstance(item["aliases"], (list, tuple))
                ):
                    raise ValueError("invalid recovered source")
                state.table.register(
                    _identity(item["canonical_id"]),
                    role="user",
                    source=item["source"],
                    confirmed=True,
                    aliases=tuple(_identity(v) for v in item["aliases"]),
                )
            state.tool_calls.update(_identity(v) for v in record["tool_calls"])
            state.completed_inputs.update(_identity(v) for v in record["completed_inputs"])
            for message_key, message in record["messages"].items():
                if not isinstance(message, dict) or set(message) != {"digest", "status", "item_id"}:
                    raise ValueError("invalid recovered message")
                digest = message["digest"]
                if (
                    not isinstance(digest, str)
                    or len(digest) != 64
                    or any(c not in "0123456789abcdef" for c in digest)
                    or not isinstance(message["status"], str)
                    or message["status"] not in {"pending", "confirmed"}
                ):
                    raise ValueError("invalid recovered message state")
                item_id = message["item_id"]
                if message["status"] == "confirmed" and (
                    not isinstance(item_id, str) or state.table.lookup(item_id) is None
                ):
                    raise ValueError("unregistered recovered message")
                state.messages[_identity(message_key)] = dict(message)
        return result
