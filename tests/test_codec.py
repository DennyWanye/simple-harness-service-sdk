from __future__ import annotations

import pytest

from simple_harness_service import FRAME_MAX_BYTES, ServiceError
from simple_harness_service.codec import FrameDecoder, encode_frame


def test_fragmented_and_coalesced_frames() -> None:
    first = encode_frame({"a": 1})
    second = encode_frame({"b": 2})
    decoder = FrameDecoder()
    assert decoder.feed(first[:2]) == []
    assert decoder.feed(first[2:] + second) == [{"a": 1}, {"b": 2}]


def test_oversize_length_rejected_before_payload() -> None:
    decoder = FrameDecoder()
    with pytest.raises(ServiceError):
        decoder.feed((FRAME_MAX_BYTES + 1).to_bytes(4, "big"))


def test_canonical_encoding_is_deterministic() -> None:
    assert encode_frame({"b": 2, "a": 1}) == encode_frame({"a": 1, "b": 2})

