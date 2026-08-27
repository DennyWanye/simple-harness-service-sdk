# Qwen native authority revision proposal

Status: accepted by the already-approved `FAIL-PROTOCOL-DRIFT` stop condition and implemented as a new immutable candidate. The published `v0.3.0` artifacts and `qwen-native/2026-08-27.1` bytes remain unchanged.

## Trigger

The S2 production preflight connected successfully to the official Beijing workspace-scoped WSS endpoint with a pooled DashScope credential, but received no server event for 12 seconds when waiting for a server-first `session.created`.

Two follow-up metadata-only probes sent no audio, transcript, or user content:

1. WebSocket open with no client frame: no server event in 12 seconds.
2. WebSocket open followed immediately by `session.update`: immediate `session.updated`.

The exact SDK nested audio request was accepted. The acknowledgement was nested and contained `id`, `modalities`, `instructions`, `voice`, `audio.input/output.format`, `input_audio_transcription`, and `turn_detection`; it did not contain the frozen `object`, `model`, `input_audio_format`, or `output_audio_format` fields.

This conflicts with the published `qwen-native/2026-08-27.1` bootstrap and acknowledgement fixtures. It is not a network, credential, quota, or SSH failure.

## Versioned resolution

- Service SDK candidate: `0.3.1`.
- Active Qwen wire authority: `qwen-native/2026-08-28.1`.
- Active relay control authority: `tokenseller.realtime-control/2026-08-28.1`.
- Historical authority directories remain byte-identical in source history; release packaging selects exactly one active pack per authority role.
- Relay admission completes the Provider socket connection without waiting for a Provider frame, emits `tokenseller.session.created`, then forwards the SDK-owned native `session.update`.
- `session.updated` is the sole Qwen ready acknowledgement for this wire version.
- No Qwen event is renamed or translated by TokenSeller.

## Evidence and remaining gate

- Official API overview: <https://help.aliyun.com/en/model-studio/realtime>
- Official client events: <https://help.aliyun.com/en/model-studio/client-events>
- Official server events: <https://help.aliyun.com/en/model-studio/server-events>
- Live probes were metadata-only and emitted no credential, content, or audio artifacts.
- The revision is not releasable until SDK unit/conformance/packaging gates, TokenSeller cross-language authority checks, and a paid multi-turn production smoke all pass.
