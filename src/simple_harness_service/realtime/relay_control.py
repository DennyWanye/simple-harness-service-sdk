"""Codec for TokenSeller's versioned Realtime control plane."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from functools import lru_cache
from importlib.resources import files
from typing import Any

from .adapters._shared import strict_json_object
from .contracts import (
    CloseDisposition,
    CloseInitiator,
    MintedRealtimeCredential,
    RealtimeAudioFormat,
    RealtimeCapability,
    RealtimeError,
    RealtimeErrorCode,
    RealtimeFeature,
    RealtimeFeatureSet,
    RealtimeLimits,
    RealtimeOpenRequest,
    RealtimeProfile,
    SessionClosed,
    SessionExpiring,
    SessionFailed,
)

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_ID = re.compile(r"^[\x21-\x7e]{1,128}$")
_REVISION_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_MESSAGE_CLASS = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MESSAGE_CLASSES = frozenset(
    {
        "capability_digest_mismatch",
        "version_mismatch",
        "invalid_open",
        "auth_failed",
        "forbidden",
        "capacity_busy",
        "rate_limited",
        "upstream_unavailable",
        "upstream_protocol_error",
        "session_expired",
        "billing_rejected",
        "internal_error",
    }
)
_ACTIVE_RESPONSES = frozenset({"none", "completed", "cancelled"})
_SETTLEMENTS = frozenset({"none", "settled", "released", "deferred"})
_CONTROL_ERROR_CODES = frozenset(
    {
        "invalid_request",
        "unauthenticated",
        "forbidden",
        "unsupported",
        "busy",
        "rate_limited",
        "unavailable",
        "timeout",
        "protocol_error",
        "session_expired",
        "billing_rejected",
        "internal",
    }
)


def _reject_non_integer_numbers(value: object) -> None:
    if isinstance(value, float):
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "non-integer JSON number")
    if isinstance(value, dict):
        for item in value.values():
            _reject_non_integer_numbers(item)
    elif isinstance(value, list):
        for item in value:
            _reject_non_integer_numbers(item)


def canonical_json(value: Mapping[str, object]) -> bytes:
    _reject_non_integer_numbers(value)
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "non-canonical JSON") from error


def capability_digest(value: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, f"{name} must be an object")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, f"{name} is required")
    return value


def _integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, f"{name} must be an integer")
    return value


def _opaque_id(value: object, name: str) -> str:
    identifier = _string(value, name)
    if _OPAQUE_ID.fullmatch(identifier) is None or len(identifier.encode("utf-8")) > 128:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, f"{name} is invalid")
    return identifier


def _exact_keys(value: Mapping[str, object], required: frozenset[str]) -> None:
    if frozenset(value) != required:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "control fields do not match")


def parse_capability(
    document: Mapping[str, object], profile: RealtimeProfile
) -> RealtimeCapability:
    _validate_capability_authority(document)
    static = {
        "control_version": profile.capability.control_version,
        "sdk_protocol_version": profile.capability.sdk_protocol_version,
        "provider": profile.provider,
        "wire_protocol": profile.wire_protocol,
        "wire_version": profile.wire_version,
        "public_model": profile.public_model,
        "voice": profile.voice,
    }
    for key, expected in static.items():
        if document.get(key) != expected:
            raise RealtimeError(RealtimeErrorCode.UNSUPPORTED, f"capability {key} mismatch")
    audio = _object(document.get("audio"), "audio")
    input_audio = _audio_format(_object(audio.get("input"), "audio.input"))
    output_audio = _audio_format(_object(audio.get("output"), "audio.output"))
    raw_features = _object(document.get("features"), "features")
    feature_values: dict[str, bool] = {}
    for feature in RealtimeFeature:
        raw = raw_features.get(feature.value)
        if not isinstance(raw, bool):
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "invalid feature set")
        feature_values[feature.value] = raw
    raw_limits = _object(document.get("limits"), "limits")
    limits = RealtimeLimits(
        json_frame_bytes=_integer(raw_limits.get("json_frame_bytes"), "json_frame_bytes"),
        input_audio_event_bytes=_integer(
            raw_limits.get("input_audio_event_decoded_bytes"), "input_audio_event_decoded_bytes"
        ),
        output_audio_event_bytes=_integer(
            raw_limits.get("output_audio_event_decoded_bytes"), "output_audio_event_decoded_bytes"
        ),
        input_queue_frames=_integer(raw_limits.get("input_queue_frames"), "input_queue_frames"),
        input_queue_bytes=_integer(raw_limits.get("input_queue_bytes"), "input_queue_bytes"),
        output_queue_frames=_integer(raw_limits.get("output_queue_frames"), "output_queue_frames"),
        output_queue_bytes=_integer(raw_limits.get("output_queue_bytes"), "output_queue_bytes"),
        tool_payload_bytes=_integer(raw_limits.get("tool_payload_bytes"), "tool_payload_bytes"),
    )
    capability = RealtimeCapability(
        control_version=profile.capability.control_version,
        sdk_protocol_version=profile.capability.sdk_protocol_version,
        provider=profile.provider,
        wire_protocol=profile.wire_protocol,
        wire_version=profile.wire_version,
        input_audio=input_audio,
        output_audio=output_audio,
        features=RealtimeFeatureSet(**feature_values),
        limits=limits,
    )
    if capability.input_audio != profile.capability.input_audio:
        raise RealtimeError(RealtimeErrorCode.UNSUPPORTED, "input audio mismatch")
    if capability.output_audio != profile.capability.output_audio:
        raise RealtimeError(RealtimeErrorCode.UNSUPPORTED, "output audio mismatch")
    return capability


@lru_cache(maxsize=1)
def _capability_authority() -> dict[str, Any]:
    resource = files("simple_harness_service.realtime").joinpath(
        "protocols",
        "tokenseller-realtime-control-2026-08-28.3",
        "capability-manifest.json",
    )
    return strict_json_object(resource.read_text(encoding="utf-8"))


def _validate_capability_authority(document: Mapping[str, object]) -> None:
    wallet_revision = _string(
        document.get("wallet_pricing_revision"), "wallet_pricing_revision"
    )
    wallet_digest = _string(
        document.get("wallet_pricing_revision_digest"),
        "wallet_pricing_revision_digest",
    )
    if _REVISION_ID.fullmatch(wallet_revision) is None:
        raise RealtimeError(
            RealtimeErrorCode.PROTOCOL_ERROR,
            "wallet pricing revision is invalid",
        )
    if _DIGEST.fullmatch(wallet_digest) is None:
        raise RealtimeError(
            RealtimeErrorCode.PROTOCOL_ERROR,
            "wallet pricing revision digest is invalid",
        )
    expected = dict(_capability_authority())
    expected["wallet_pricing_revision"] = wallet_revision
    expected["wallet_pricing_revision_digest"] = wallet_digest
    if dict(document) != expected:
        raise RealtimeError(
            RealtimeErrorCode.PROTOCOL_ERROR,
            "capability does not match packaged authority",
        )


def _audio_format(value: Mapping[str, object]) -> RealtimeAudioFormat:
    return RealtimeAudioFormat(
        codec=_string(value.get("codec"), "codec"),
        sample_rate=_integer(value.get("sample_rate"), "sample_rate"),
        channels=_integer(value.get("channels"), "channels"),
    )


class RelayControlCodec:
    def build_mint_request(
        self,
        profile: RealtimeProfile,
        request: RealtimeOpenRequest,
        correlation: str,
    ) -> dict[str, object]:
        return {
            "provider": profile.provider,
            "wire_protocol": profile.wire_protocol,
            "wire_version": profile.wire_version,
            "sdk_protocol_version": profile.capability.sdk_protocol_version,
            "public_model": profile.public_model,
            "voice": profile.voice,
            "correlation": correlation,
            "required_features": sorted(feature.value for feature in request.required_features),
            "audio": {
                "input": self._audio_document(request.input_audio),
                "output": self._audio_document(request.output_audio),
            },
        }

    def parse_mint_response(
        self,
        payload: str | Mapping[str, object],
        profile: RealtimeProfile,
    ) -> MintedRealtimeCredential:
        value = strict_json_object(payload) if isinstance(payload, str) else dict(payload)
        _exact_keys(
            value,
            frozenset({"client_secret", "websocket_path", "capability", "capability_digest"}),
        )
        secret = _object(value.get("client_secret"), "client_secret")
        _exact_keys(secret, frozenset({"value", "expires_at_ms"}))
        document = _object(value.get("capability"), "capability")
        digest = _string(value.get("capability_digest"), "capability_digest")
        if not _DIGEST.fullmatch(digest) or capability_digest(document) != digest:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "capability digest mismatch")
        capability = parse_capability(document, profile)
        return MintedRealtimeCredential(
            secret=_string(secret.get("value"), "client_secret.value"),
            expires_at_ms=_integer(secret.get("expires_at_ms"), "expires_at_ms"),
            websocket_path=_string(value.get("websocket_path"), "websocket_path"),
            capability=capability,
            capability_document=document,
            capability_digest=digest,
        )

    def validate_minted(
        self,
        minted: MintedRealtimeCredential,
        profile: RealtimeProfile,
        request: RealtimeOpenRequest,
    ) -> None:
        parsed = parse_capability(minted.capability_document, profile)
        if parsed != minted.capability:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "minted capability mismatch")
        if capability_digest(minted.capability_document) != minted.capability_digest:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "capability digest mismatch")
        if not minted.capability.features.supports(request.required_features):
            raise RealtimeError(RealtimeErrorCode.UNSUPPORTED, "required feature unavailable")
        if request.input_audio != minted.capability.input_audio:
            raise RealtimeError(RealtimeErrorCode.UNSUPPORTED, "input audio unavailable")
        if request.output_audio != minted.capability.output_audio:
            raise RealtimeError(RealtimeErrorCode.UNSUPPORTED, "output audio unavailable")

    def build_session_open(self, credential: MintedRealtimeCredential, correlation: str) -> str:
        payload, _event_id = self._build_bound_session_open(credential, correlation)
        return payload

    def _build_bound_session_open(
        self,
        credential: MintedRealtimeCredential,
        correlation: str,
    ) -> tuple[str, str]:
        event_id = f"ctl_open_{uuid.uuid4().hex}"
        return json.dumps(
            {
                "type": "tokenseller.session.open",
                "event_id": event_id,
                "control_version": credential.capability.control_version,
                "sdk_protocol_version": credential.capability.sdk_protocol_version,
                "correlation": correlation,
                "capability_digest": credential.capability_digest,
            },
            separators=(",", ":"),
        ), event_id

    def validate_session_created(
        self,
        payload: str,
        credential: MintedRealtimeCredential,
        related_event_id: str,
    ) -> None:
        value = strict_json_object(payload)
        if value.get("type") != "tokenseller.session.created":
            if value.get("type") == "tokenseller.error":
                failure = self.decode_runtime_event(payload)
                if isinstance(failure, SessionFailed):
                    raise RealtimeError(failure.code, retryable=failure.retryable)
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "session.created required")
        _exact_keys(
            value,
            frozenset(
                {
                    "type",
                    "event_id",
                    "related_event_id",
                    "control_version",
                    "sdk_protocol_version",
                    "relay_session_id",
                    "provider",
                    "wire_protocol",
                    "wire_version",
                    "capability",
                    "capability_digest",
                    "expires_at_ms",
                }
            ),
        )
        _opaque_id(value.get("event_id"), "event_id")
        created_related_event_id = _opaque_id(
            value.get("related_event_id"), "related_event_id"
        )
        _opaque_id(value.get("relay_session_id"), "relay_session_id")
        if created_related_event_id != _opaque_id(related_event_id, "expected related_event_id"):
            raise RealtimeError(
                RealtimeErrorCode.PROTOCOL_ERROR,
                "session.created related event mismatch",
            )
        if value.get("control_version") != credential.capability.control_version:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "control version mismatch")
        if value.get("sdk_protocol_version") != credential.capability.sdk_protocol_version:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "SDK version mismatch")
        if value.get("provider") != credential.capability.provider:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "provider mismatch")
        if value.get("wire_protocol") != credential.capability.wire_protocol:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "wire protocol mismatch")
        if value.get("wire_version") != credential.capability.wire_version:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "wire version mismatch")
        if _integer(value.get("expires_at_ms"), "expires_at_ms") != credential.expires_at_ms:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "expiry mismatch")
        document = _object(value.get("capability"), "capability")
        if document != credential.capability_document:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "created capability mismatch")
        if value.get("capability_digest") != credential.capability_digest:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "created digest mismatch")

    def build_session_close(self, reason: str) -> str:
        payload, _event_id = self._build_bound_session_close(reason)
        return payload

    def _build_bound_session_close(self, reason: str) -> tuple[str, str]:
        if reason not in {"client_hangup", "app_shutdown"}:
            raise RealtimeError(RealtimeErrorCode.INVALID_REQUEST, "invalid close reason")
        event_id = f"ctl_close_{uuid.uuid4().hex}"
        return json.dumps(
            {
                "type": "tokenseller.session.close",
                "event_id": event_id,
                "reason": reason,
            },
            separators=(",", ":"),
        ), event_id

    def decode_runtime_event(
        self,
        payload: str,
    ) -> SessionExpiring | SessionClosed | SessionFailed | None:
        value = strict_json_object(payload)
        event_type = value.get("type")
        if not isinstance(event_type, str) or not event_type.startswith("tokenseller."):
            return None
        if event_type == "tokenseller.session.expiring":
            _exact_keys(value, frozenset({"type", "event_id", "remaining_ms"}))
            _opaque_id(value.get("event_id"), "event_id")
            return SessionExpiring(_integer(value.get("remaining_ms"), "remaining_ms"))
        if event_type == "tokenseller.error":
            required = frozenset(
                {"type", "event_id", "code", "retryable", "fatal", "message_class"}
            )
            allowed = frozenset(
                {
                "type",
                "event_id",
                "related_event_id",
                "code",
                "retryable",
                "fatal",
                "message_class",
                "retry_after_ms",
                }
            )
            if not required.issubset(value) or not set(value).issubset(allowed):
                raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "control fields do not match")
            _opaque_id(value.get("event_id"), "event_id")
            if "related_event_id" in value:
                _opaque_id(value.get("related_event_id"), "related_event_id")
            try:
                raw_code = _string(value.get("code"), "code")
                if raw_code not in _CONTROL_ERROR_CODES:
                    raise ValueError(raw_code)
                code = RealtimeErrorCode(raw_code)
            except ValueError as error:
                raise RealtimeError(
                    RealtimeErrorCode.PROTOCOL_ERROR, "unknown error code"
                ) from error
            retryable = value.get("retryable")
            if not isinstance(retryable, bool):
                raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "retryable must be bool")
            fatal = value.get("fatal")
            if fatal is not True:
                raise RealtimeError(
                    RealtimeErrorCode.PROTOCOL_ERROR,
                    "non-terminal control errors are unsupported",
                )
            message_class = _string(value.get("message_class"), "message_class")
            if (
                _MESSAGE_CLASS.fullmatch(message_class) is None
                or message_class not in _MESSAGE_CLASSES
            ):
                raise RealtimeError(
                    RealtimeErrorCode.PROTOCOL_ERROR,
                    "unknown message class",
                )
            if "retry_after_ms" in value:
                retry_after_ms = _integer(value.get("retry_after_ms"), "retry_after_ms")
                if retry_after_ms > 3_600_000:
                    raise RealtimeError(
                        RealtimeErrorCode.PROTOCOL_ERROR,
                        "retry_after_ms is out of range",
                    )
            return SessionFailed(code, retryable)
        if event_type == "tokenseller.session.closed":
            _exact_keys(
                value,
                frozenset(
                    {
                        "type",
                        "event_id",
                        "related_event_id",
                        "initiator",
                        "disposition",
                        "active_response",
                        "settlement",
                    }
                ),
            )
            _opaque_id(value.get("event_id"), "event_id")
            _opaque_id(value.get("related_event_id"), "related_event_id")
            try:
                initiator = CloseInitiator(_string(value.get("initiator"), "initiator"))
                disposition = CloseDisposition(_string(value.get("disposition"), "disposition"))
            except ValueError as error:
                raise RealtimeError(
                    RealtimeErrorCode.PROTOCOL_ERROR, "unknown close enum"
                ) from error
            if value.get("active_response") not in _ACTIVE_RESPONSES:
                raise RealtimeError(
                    RealtimeErrorCode.PROTOCOL_ERROR,
                    "unknown active response disposition",
                )
            if value.get("settlement") not in _SETTLEMENTS:
                raise RealtimeError(
                    RealtimeErrorCode.PROTOCOL_ERROR,
                    "unknown settlement disposition",
                )
            return SessionClosed(
                "session_expired"
                if disposition is CloseDisposition.CLEAN_EXPIRED
                else "session_closed",
                initiator,
                disposition,
                active_response=_string(value.get("active_response"), "active_response"),
                settlement=_string(value.get("settlement"), "settlement"),
                related_event_id=_opaque_id(
                    value.get("related_event_id"), "related_event_id"
                ),
            )
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "unknown control event")

    @staticmethod
    def _audio_document(value: RealtimeAudioFormat) -> dict[str, object]:
        return {"codec": value.codec, "sample_rate": value.sample_rate, "channels": value.channels}
