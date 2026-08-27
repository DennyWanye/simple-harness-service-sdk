from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from simple_harness_service.realtime.connectors.tokenseller import (
    NATIVE_MINT_PATH,
    TokenSellerConnectorError,
    TokenSellerHttpsCredentialMinter,
)
from simple_harness_service.realtime.contracts import (
    MintedRealtimeCredential,
    RealtimeAudioFormat,
    RealtimeCapability,
    RealtimeFeature,
    RealtimeFeatureSet,
    RealtimeLimits,
    RealtimeOpenRequest,
    RealtimeProfile,
)
from simple_harness_service.realtime.ports import CredentialMinter

_CONTROL = (
    Path(__file__).parents[1]
    / "src/simple_harness_service/realtime/protocols/"
    "tokenseller-realtime-control-2026-08-28.3"
)


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        value: object,
        *,
        chunks: list[bytes] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.content = json.dumps(value, separators=(",", ":")).encode()
        self.chunks = chunks
        self.headers = dict(headers or {})

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks if self.chunks is not None else [self.content]:
            yield chunk


class FakeStream:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    async def __aenter__(self) -> FakeResponse:
        return self.response

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


class FakeHttpClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, Mapping[str, str], Mapping[str, object], float]] = []
        self.closed = 0

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object],
        timeout: float,
    ) -> Any:
        assert method == "POST"
        self.calls.append((url, headers, json, timeout))
        return FakeStream(self.response)

    async def aclose(self) -> None:
        self.closed += 1


def _profile() -> RealtimeProfile:
    return RealtimeProfile(
        name="qwen-production",
        provider="qwen",
        wire_protocol="qwen-native",
        wire_version="2026-08-28.3",
        public_model="qwen3.5-omni-realtime",
        voice="Tina",
        capability=RealtimeCapability(
            control_version="2026-08-28.3",
            sdk_protocol_version="simple-harness-realtime/1",
            provider="qwen",
            wire_protocol="qwen-native",
            wire_version="2026-08-28.3",
            input_audio=RealtimeAudioFormat(),
            output_audio=RealtimeAudioFormat(sample_rate=24_000),
            features=RealtimeFeatureSet(
                server_turn_detection=True,
                automatic_response=True,
                interruption=True,
                input_transcription=True,
                text_output=True,
                audio_output=True,
                cancel_response=True,
                tool_calling=True,
            ),
            limits=RealtimeLimits(),
        ),
    )


def _request() -> RealtimeOpenRequest:
    return RealtimeOpenRequest(
        external_session_id="product-session",
        instructions="Answer briefly.",
        required_features=frozenset(
            {RealtimeFeature.SERVER_TURN_DETECTION, RealtimeFeature.AUDIO_OUTPUT}
        ),
    )


def _mint_response() -> dict[str, object]:
    value = json.loads((_CONTROL / "mint-response.json").read_text())
    assert isinstance(value, dict)
    return value


@pytest.mark.asyncio
async def test_minter_builds_exact_request_and_redacts_secrets() -> None:
    client = FakeHttpClient(FakeResponse(200, _mint_response()))
    minter = TokenSellerHttpsCredentialMinter(
        "https://relay.example", "tsk_super_secret", client=client
    )

    credential = await minter.mint(
        _profile(), _request(), "corr_0123456789ABCDEFGHJKMNPQRS"
    )

    assert isinstance(credential, MintedRealtimeCredential)
    assert credential.websocket_path == "/v1/realtime/qwen"
    assert credential.secret == "eph_fixture_redacted"
    url, headers, payload, timeout = client.calls[0]
    assert url == f"https://relay.example{NATIVE_MINT_PATH}"
    assert headers["Authorization"] == "Bearer tsk_super_secret"
    assert payload["required_features"] == ["audio_output", "server_turn_detection"]
    assert payload["audio"] == {
        "input": {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1},
        "output": {"codec": "pcm_s16le", "sample_rate": 24000, "channels": 1},
    }
    assert timeout == 10.0
    assert "tsk_super_secret" not in repr(minter)
    assert "eph_fixture_redacted" not in repr(credential)

    await minter.aclose()
    await minter.aclose()
    assert client.closed == 0  # caller-owned clients remain caller-owned


@pytest.mark.asyncio
async def test_minter_rejects_cross_origin_path_and_hides_remote_body() -> None:
    invalid = _mint_response()
    invalid["websocket_path"] = "wss://attacker.example/v1/realtime/qwen"
    client = FakeHttpClient(FakeResponse(200, invalid))
    minter = TokenSellerHttpsCredentialMinter(
        "https://relay.example", "tsk_do_not_log", client=client
    )

    with pytest.raises(TokenSellerConnectorError, match="protocol_error") as raised:
        await minter.mint(_profile(), _request(), "corr_0123456789ABCDEFGHJKMNPQRS")
    assert "attacker" not in str(raised.value)
    assert "tsk_do_not_log" not in str(raised.value)

    failing = FakeHttpClient(FakeResponse(401, {"secret": "provider-body-secret"}))
    minter = TokenSellerHttpsCredentialMinter(
        "https://relay.example", "tsk_do_not_log", client=failing
    )
    with pytest.raises(TokenSellerConnectorError, match="unauthenticated") as raised:
        await minter.mint(_profile(), _request(), "corr_0123456789ABCDEFGHJKMNPQRS")
    assert "provider-body-secret" not in str(raised.value)


@pytest.mark.asyncio
async def test_minter_rejects_duplicate_response_keys_before_admission() -> None:
    response = FakeResponse(200, {})
    response.content = (
        b'{"client_secret":{"value":"first","value":"second",'
        b'"expires_at_ms":1787875260000},"websocket_path":"/v1/realtime/qwen",'
        b'"capability":{},"capability_digest":"'
        + b"0" * 64
        + b'"}'
    )
    client = FakeHttpClient(response)
    minter = TokenSellerHttpsCredentialMinter(
        "https://relay.example", "tsk_do_not_log", client=client
    )

    with pytest.raises(TokenSellerConnectorError, match="protocol_error"):
        await minter.mint(_profile(), _request(), "corr_0123456789ABCDEFGHJKMNPQRS")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(
            200,
            {},
            chunks=[b"x"],
            headers={"Content-Length": str(1_048_577)},
        ),
        FakeResponse(
            200,
            {},
            chunks=[b"x" * 700_000, b"x" * 400_000],
        ),
        FakeResponse(
            200,
            {},
            chunks=[b"{}"],
            headers={"Content-Length": "1"},
        ),
    ],
)
async def test_minter_streaming_body_is_bounded_and_length_checked(
    response: FakeResponse,
) -> None:
    minter = TokenSellerHttpsCredentialMinter(
        "https://relay.example", "tsk_do_not_log", client=FakeHttpClient(response)
    )

    with pytest.raises(TokenSellerConnectorError, match="protocol_error"):
        await minter.mint(_profile(), _request(), "corr_0123456789ABCDEFGHJKMNPQRS")


def test_minter_requires_an_https_origin() -> None:
    with pytest.raises(ValueError, match="HTTPS origin"):
        TokenSellerHttpsCredentialMinter("http://relay.example", "tsk_value")
    with pytest.raises(ValueError, match="HTTPS origin"):
        TokenSellerHttpsCredentialMinter("https://relay.example/base", "tsk_value")
    with pytest.raises(ValueError, match="HTTPS origin"):
        TokenSellerHttpsCredentialMinter(
            "https://user:secret@relay.example/?token=secret", "tsk_value"
        )
    with pytest.raises(ValueError, match="HTTPS origin"):
        TokenSellerHttpsCredentialMinter("https:\\relay.example", "tsk_value")


def test_minter_canonicalizes_origin_without_secret_bearing_components() -> None:
    minter = TokenSellerHttpsCredentialMinter(
        "HTTPS://Relay.Example:443/", "tsk_value", client=FakeHttpClient(FakeResponse(500, {}))
    )
    assert "https://relay.example" in repr(minter)


def test_minter_matches_the_public_port_shape() -> None:
    client = FakeHttpClient(FakeResponse(200, _mint_response()))
    minter: CredentialMinter = TokenSellerHttpsCredentialMinter(
        "https://relay.example", "tsk_value", client=client
    )
    assert minter is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("provider-body-secret tsk_do_not_log"),
        httpx.ReadTimeout("provider-body-secret tsk_do_not_log"),
    ],
)
async def test_minter_timeout_is_retryable_stable_and_redacted(
    failure: Exception,
) -> None:
    class RaisingStream:
        async def __aenter__(self) -> FakeResponse:
            raise failure

        async def __aexit__(
            self, exc_type: object, exc: object, traceback: object
        ) -> None:
            return None

    class RaisingHttpClient(FakeHttpClient):
        def stream(
            self,
            method: str,
            url: str,
            *,
            headers: Mapping[str, str],
            json: Mapping[str, object],
            timeout: float,
        ) -> Any:
            self.calls.append((url, headers, json, timeout))
            return RaisingStream()

    minter = TokenSellerHttpsCredentialMinter(
        "https://relay.example",
        "tsk_do_not_log",
        client=RaisingHttpClient(FakeResponse(200, _mint_response())),
    )

    with pytest.raises(TokenSellerConnectorError, match=r"^timeout$") as caught:
        await minter.mint(_profile(), _request(), "corr_0123456789ABCDEFGHJKMNPQRS")
    assert caught.value.code.value == "timeout"
    assert caught.value.retryable
    assert "provider-body-secret" not in str(caught.value)
    assert "tsk_do_not_log" not in str(caught.value)
