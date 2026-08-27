from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from simple_harness_service.realtime.adapters.qwen_omni import (
    QWEN_CAPABILITY,
    QwenOmniAdapter,
)
from simple_harness_service.realtime.contracts import (
    CloseDisposition,
    CloseInitiator,
    RealtimeError,
    RealtimeErrorCode,
    RealtimeFeature,
    RealtimeOpenRequest,
    RealtimeProfile,
    SessionClosed,
    SessionExpiring,
    SessionFailed,
)
from simple_harness_service.realtime.local import (
    AudioDirection,
    LocalPcmFrame,
    SequenceWindow,
    decode_local_message,
    decode_pcm_frame,
    encode_pcm_frame,
)
from simple_harness_service.realtime.relay_control import (
    RelayControlCodec,
    capability_digest,
)

ROOT = Path(__file__).parents[1]
CONTROL = ROOT / "ARCHITECTURE/protocols/tokenseller-realtime-control-2026-08-28.2"


def _profile() -> RealtimeProfile:
    return RealtimeProfile(
        "qwen-production",
        "qwen",
        "qwen-native",
        "2026-08-28.2",
        "qwen3.5-omni-realtime",
        "Tina",
        QWEN_CAPABILITY,
    )


def test_product_open_request_is_provider_neutral_and_feature_closed() -> None:
    request = RealtimeOpenRequest(
        "session-1",
        "Be concise.",
        frozenset({RealtimeFeature.AUDIO_OUTPUT, RealtimeFeature.INTERRUPTION}),
    )
    assert "provider" not in request.__dataclass_fields__
    assert QwenOmniAdapter().capability.features.supports(request.required_features)
    with pytest.raises(ValueError):
        RealtimeOpenRequest("", "instructions")


def test_control_authority_mint_and_created_vectors_are_digest_bound() -> None:
    codec = RelayControlCodec()
    mint = (CONTROL / "mint-response.json").read_text()
    credential = codec.parse_mint_response(mint, _profile())
    assert capability_digest(credential.capability_document) == credential.capability_digest
    created_payload = (CONTROL / "server-session-created.json").read_text()
    created = json.loads(created_payload)
    codec.validate_session_created(
        created_payload, credential, created["related_event_id"]
    )
    request = RealtimeOpenRequest("session-1", "Be concise.")
    codec.validate_minted(credential, _profile(), request)

    tampered = json.loads(mint)
    tampered["capability"]["voice"] = "Other"
    with pytest.raises(RealtimeError) as caught:
        codec.parse_mint_response(tampered, _profile())
    assert caught.value.code is RealtimeErrorCode.PROTOCOL_ERROR


def test_minted_credential_repr_exception_and_log_never_contain_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    credential = RelayControlCodec().parse_mint_response(
        (CONTROL / "mint-response.json").read_text(), _profile()
    )

    rendered = repr(credential)
    error = RuntimeError(credential)
    with caplog.at_level(logging.WARNING):
        logging.getLogger("realtime-credential-test").warning("credential=%r", credential)

    assert credential.secret not in rendered
    assert credential.secret not in str(error)
    assert credential.secret not in caplog.text
    assert "<redacted>" in rendered


def test_control_open_identity_is_bound_and_public_builder_stays_compatible() -> None:
    codec = RelayControlCodec()
    credential = codec.parse_mint_response(
        (CONTROL / "mint-response.json").read_text(), _profile()
    )
    correlation = "corr_0123456789ABCDEFGHJKMNPQRS"

    public_payload = codec.build_session_open(credential, correlation)
    bound_payload, event_id = codec._build_bound_session_open(credential, correlation)

    assert isinstance(public_payload, str)
    assert json.loads(bound_payload)["event_id"] == event_id


@pytest.mark.parametrize("field", ["event_id", "related_event_id", "relay_session_id"])
def test_created_rejects_empty_opaque_identity_fields(field: str) -> None:
    codec = RelayControlCodec()
    credential = codec.parse_mint_response(
        (CONTROL / "mint-response.json").read_text(), _profile()
    )
    created = json.loads((CONTROL / "server-session-created.json").read_text())
    expected_related_event_id = created["related_event_id"]
    created[field] = ""

    with pytest.raises(RealtimeError) as caught:
        codec.validate_session_created(
            json.dumps(created), credential, expected_related_event_id
        )

    assert caught.value.code is RealtimeErrorCode.PROTOCOL_ERROR


def test_created_rejects_mismatched_related_open_event() -> None:
    codec = RelayControlCodec()
    credential = codec.parse_mint_response(
        (CONTROL / "mint-response.json").read_text(), _profile()
    )

    with pytest.raises(RealtimeError) as caught:
        codec.validate_session_created(
            (CONTROL / "server-session-created.json").read_text(),
            credential,
            "ctl_open_different",
        )

    assert caught.value.code is RealtimeErrorCode.PROTOCOL_ERROR


@pytest.mark.parametrize(
    "mutation",
    [
        lambda capability: capability.__setitem__("upstream_model", "drifted-model"),
        lambda capability: capability.__setitem__(
            "provider_cost_revision", "drifted-provider-cost"
        ),
        lambda capability: capability["session"].__setitem__("ephemeral_ttl_ms", 1),
        lambda capability: capability["limits"].__setitem__("unknown_limit", 1),
        lambda capability: capability["audio"]["input"].__setitem__("unknown", True),
    ],
)
def test_capability_rejects_self_signed_static_or_nested_drift(
    mutation: Callable[[dict[str, Any]], object],
) -> None:
    mint = json.loads((CONTROL / "mint-response.json").read_text())
    mutation(mint["capability"])
    mint["capability_digest"] = capability_digest(mint["capability"])

    with pytest.raises(RealtimeError) as caught:
        RelayControlCodec().parse_mint_response(mint, _profile())

    assert caught.value.code is RealtimeErrorCode.PROTOCOL_ERROR


def test_capability_allows_same_schema_dynamic_wallet_revision_binding() -> None:
    mint = json.loads((CONTROL / "mint-response.json").read_text())
    mint["capability"]["wallet_pricing_revision"] = "tokenseller-qwen35-usd-v2"
    mint["capability"]["wallet_pricing_revision_digest"] = "a" * 64
    mint["capability_digest"] = capability_digest(mint["capability"])

    credential = RelayControlCodec().parse_mint_response(mint, _profile())

    assert credential.capability_digest == mint["capability_digest"]


def test_runtime_control_authority_vectors_decode_strictly() -> None:
    codec = RelayControlCodec()

    error = codec.decode_runtime_event((CONTROL / "server-error.json").read_text())
    expiring = codec.decode_runtime_event(
        (CONTROL / "server-session-expiring.json").read_text()
    )
    closed = codec.decode_runtime_event(
        (CONTROL / "server-session-closed.json").read_text()
    )

    assert error == SessionFailed(RealtimeErrorCode.PROTOCOL_ERROR, False)
    assert expiring == SessionExpiring(10_000)
    assert closed == SessionClosed(
        "session_closed",
        CloseInitiator.CLIENT,
        CloseDisposition.CLEAN,
        active_response="cancelled",
        settlement="released",
        related_event_id="ctl_close_00000000000000000000001",
    )


@pytest.mark.parametrize(
    ("mutation", "fixture"),
    [
        (lambda value: value.pop("fatal"), "server-error.json"),
        (lambda value: value.pop("message_class"), "server-error.json"),
        (lambda value: value.__setitem__("event_id", ""), "server-error.json"),
        (lambda value: value.__setitem__("fatal", False), "server-error.json"),
        (
            lambda value: value.__setitem__("message_class", "not_frozen"),
            "server-error.json",
        ),
        (lambda value: value.__setitem__("retry_after_ms", -1), "server-error.json"),
        (
            lambda value: value.__setitem__("retry_after_ms", 3_600_001),
            "server-error.json",
        ),
        (
            lambda value: value.__setitem__("related_event_id", ""),
            "server-error.json",
        ),
        (
            lambda value: value.__setitem__("event_id", ""),
            "server-session-expiring.json",
        ),
        (
            lambda value: value.__setitem__("active_response", "unknown"),
            "server-session-closed.json",
        ),
        (
            lambda value: value.__setitem__("settlement", "unknown"),
            "server-session-closed.json",
        ),
        (
            lambda value: value.__setitem__("unknown", True),
            "server-session-closed.json",
        ),
    ],
)
def test_runtime_control_rejects_missing_unknown_or_out_of_range_fields(
    mutation: Callable[[dict[str, Any]], object],
    fixture: str,
) -> None:
    value = json.loads((CONTROL / fixture).read_text())
    mutation(value)

    with pytest.raises(RealtimeError) as caught:
        RelayControlCodec().decode_runtime_event(json.dumps(value))

    assert caught.value.code is RealtimeErrorCode.PROTOCOL_ERROR


def test_runtime_control_maps_billing_and_clean_expired_domain_enums() -> None:
    codec = RelayControlCodec()
    error = json.loads((CONTROL / "server-error.json").read_text())
    error.update(
        {
            "code": "billing_rejected",
            "message_class": "billing_rejected",
            "retryable": False,
            "fatal": True,
        }
    )
    closed = json.loads((CONTROL / "server-session-closed.json").read_text())
    closed["disposition"] = "clean_expired"

    assert codec.decode_runtime_event(json.dumps(error)) == SessionFailed(
        RealtimeErrorCode.BILLING_REJECTED, False
    )
    assert codec.decode_runtime_event(json.dumps(closed)) == SessionClosed(
        "session_expired",
        CloseInitiator.CLIENT,
        CloseDisposition.CLEAN_EXPIRED,
        active_response="cancelled",
        settlement="released",
        related_event_id="ctl_close_00000000000000000000001",
    )


def test_runtime_control_fatal_and_retryable_are_independent_booleans() -> None:
    codec = RelayControlCodec()
    value = json.loads((CONTROL / "server-error.json").read_text())
    value["retryable"] = True
    value["fatal"] = True

    assert codec.decode_runtime_event(json.dumps(value)) == SessionFailed(
        RealtimeErrorCode.PROTOCOL_ERROR,
        True,
    )

    for retryable in (False, True):
        value["retryable"] = retryable
        value["fatal"] = False
        with pytest.raises(RealtimeError) as caught:
            codec.decode_runtime_event(json.dumps(value))
        assert caught.value.code is RealtimeErrorCode.PROTOCOL_ERROR


def test_control_canonicalization_rejects_float_and_duplicate_keys() -> None:
    with pytest.raises(RealtimeError) as caught:
        capability_digest({"fraction": 1.5})
    assert caught.value.code is RealtimeErrorCode.PROTOCOL_ERROR
    with pytest.raises(RealtimeError):
        RelayControlCodec().parse_mint_response(
            '{"client_secret":{},"client_secret":{}}', _profile()
        )


def test_local_pcm_header_round_trip_and_sequence_window() -> None:
    frame = LocalPcmFrame(AudioDirection.INPUT, 1, 1, b"\x00\x01" * 320)
    encoded = encode_pcm_frame(frame)
    assert len(encoded) == 24 + 640
    assert decode_pcm_frame(encoded) == frame

    window = SequenceWindow(1, AudioDirection.INPUT)
    assert window.accept(frame)
    assert not window.accept(frame)
    assert not window.accept(LocalPcmFrame(AudioDirection.INPUT, 2, 1, b"\x00\x00"))
    with pytest.raises(RealtimeError) as caught:
        window.accept(LocalPcmFrame(AudioDirection.INPUT, 1, 66, b"\x00\x00"))
    assert caught.value.code is RealtimeErrorCode.PROTOCOL_ERROR


def test_local_codec_rejects_unknown_fields_and_bad_pcm() -> None:
    with pytest.raises(RealtimeError):
        decode_local_message(
            '{"type":"call.barge_in","generation":1,"provider":"qwen"}'
        )
    encoded = encode_pcm_frame(
        LocalPcmFrame(AudioDirection.OUTPUT, 1, 1, b"\x00\x00")
    )
    with pytest.raises(RealtimeError):
        decode_pcm_frame(encoded[:-1])
    with pytest.raises(RealtimeError):
        encode_pcm_frame(LocalPcmFrame(AudioDirection.OUTPUT, 1, 0, b"\x00\x00"))
    zero_sequence = bytearray(encoded)
    zero_sequence[12:20] = b"\0" * 8
    with pytest.raises(RealtimeError):
        decode_pcm_frame(bytes(zero_sequence))
