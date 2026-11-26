"""Turn successive h5player FLV sessions into one continuous, MSE-ready FLV.

Each live session restarts with its own ``FLV`` header and its own timestamp
base, and interleaves a proprietary metadata tag (type ``0x61``, the vendor's
``liveTimestamp`` frames). Feeding that verbatim into Home Assistant's stream
component fails twice over:

- PyAV reports "Invalid data found" and the timestamp validator drops every
  non-monotonic packet, so segments never roll and playback freezes;
- even once the timestamps are fixed, the vendor's frame framing defeats
  Home Assistant's MP4 muxer. The vendor wraps every frame tag payload as a
  4-byte frame length followed by an Annex-B byte stream, and its periodic
  sequence headers (video tags with packet type 0) arrive only *after* the
  first keyframe, so ffmpeg's FLV demuxer ends up with an hvcC-style extradata
  record built from in-band parameter sets. ffmpeg's MOV muxer converts
  Annex-B samples to length-prefixed NAL units only when the extradata itself
  starts with an Annex-B start code; with hvcC extradata it passes the samples
  through untouched, producing fMP4 whose samples mix a length prefix with
  start codes. ffmpeg plays that back fine, but browsers' MSE (hls.js in the
  Home Assistant frontend) rejects the first fragment and restarts in a loop.

:class:`FlvNormalizer` rebuilds a single continuous stream:

- repeated ``FLV`` headers are stripped,
- ``0x61`` vendor metadata tags are dropped,
- the script tag is dropped (the vendor's own ``onMetaData`` is malformed for
  ffmpeg and claims a G.711 audio track that is not forwarded; codecs are
  detected from the in-band parameter sets instead),
- audio tags are transcoded: the camera sends G.711 A-law (FLV sound format
  7), which browsers cannot take inside fMP4, so each payload is decoded to
  PCM and re-encoded as AAC-LC (see ``audio.py``) and re-wrapped as FLV AAC
  tags (sound format 10). An AAC sequence header is emitted once, before the
  first AAC frame, and exposed via :attr:`seq_header` for mid-stream viewers;
- the vendor's hvcC sequence headers are dropped,
- each frame payload is rewritten from the vendor's
  ``[4-byte length][Annex-B]`` framing to a plain Annex-B byte stream,
- a synthetic sequence header (a video tag with packet type 0 whose payload is
  the Annex-B VPS/SPS/PPS taken from the first keyframe) is emitted exactly
  once, before that keyframe. It becomes the stream's Annex-B extradata, which
  is what makes the MOV muxer convert every sample to the length-prefixed form
  MSE requires. The tag bytes are exposed via :attr:`seq_header` so the bridge
  can prepend them to subscriber history for viewers that join mid-session,
- tag timestamps are remapped onto ONE shared output clock for both tracks.
  Video and audio carry the same vendor timestamp base, so they map through
  the same anchor; per-track clamping keeps each track strictly increasing
  while the shared mapping keeps audio and video interleaved in true content
  order (separate per-track clocks can disagree by up to the drift deadband
  and would desynchronize the muxed fragments).
"""

from __future__ import annotations

import logging
import re
import time

from .audio import AacTranscoder, alaw_to_pcm, mulaw_to_pcm

_LOGGER = logging.getLogger(__name__)

TAG_AUDIO = 8
TAG_VIDEO = 9
TAG_SCRIPT = 18
TAG_VRS_META = 0x61

VIDEO_SEQ_HEADER = 0x00
VIDEO_FRAMES = 0x01

# HEVC NAL unit types carried in-band with keyframes; the synthetic sequence
# header is built from these.
NAL_VPS = 32
NAL_SPS = 33
NAL_PPS = 34

# FLV sound formats seen on the wire; only AAC passes through untouched.
SOUND_FORMAT_ALAW = 7
SOUND_FORMAT_MULAW = 8
SOUND_FORMAT_AAC = 10

AAC_TAG_HEADER = 0xAF  # AAC; real rate/channels come from the AudioSpecificConfig
AAC_SEQ_HEADER = 0x00
AAC_RAW = 0x01

# A raw timestamp step above anything a legitimate frame step produces
# (video steps ~67 ms, audio ~40 ms) means the vendor's clock jumped; it
# resets or slides by tens of seconds about once per GOP.
MAX_RAW_STEP_MS = 2_000
# The camera's raw clock does not run at wall rate (observed between 0.6x and
# 1.3x across sessions). Output timestamps are raw-paced but re-anchored on
# the wall clock whenever the two disagree by more than this; otherwise the
# HLS player's buffer either drains early (repeating stalls) or latency grows
# without bound.
DRIFT_LIMIT_MS = 1_000

FLV_HEADER = b"FLV\x01\x01\x00\x00\x00\x09\x00\x00\x00\x00"


def _tag_total(tag_type: int, size: int) -> int:
    """Return the total wire size of an FLV tag: header + payload + previous-tag size."""
    return 11 + size + 4


def _tag_timestamp(tag: bytes) -> int:
    """Return a tag's raw 32-bit timestamp."""
    return (tag[7] << 24) | (tag[4] << 16) | (tag[5] << 8) | tag[6]


def _with_timestamp(tag: bytes, timestamp_ms: int) -> bytes:
    """Return the tag with its timestamp replaced."""
    out = bytearray(tag)
    out[4] = (timestamp_ms >> 16) & 0xFF
    out[5] = (timestamp_ms >> 8) & 0xFF
    out[6] = timestamp_ms & 0xFF
    out[7] = (timestamp_ms >> 24) & 0xFF
    return bytes(out)


def _make_tag(tag_type: int, timestamp_ms: int, payload: bytes) -> bytes:
    """Build a complete FLV tag around a payload."""
    size = len(payload)
    header = bytes(
        (
            tag_type,
            (size >> 16) & 0xFF,
            (size >> 8) & 0xFF,
            size & 0xFF,
            (timestamp_ms >> 16) & 0xFF,
            (timestamp_ms >> 8) & 0xFF,
            timestamp_ms & 0xFF,
            (timestamp_ms >> 24) & 0xFF,
            0,
            0,
            0,
        )
    )
    return header + payload + size.to_bytes(4, "big")


def _split_annexb(data: bytes) -> list[bytes]:
    """Split an Annex-B byte stream into NAL units (start codes removed)."""
    positions = [match.start() for match in re.finditer(b"\x00\x00\x01", data)]
    nals = []
    for i, pos in enumerate(positions):
        start = pos + 3
        end = positions[i + 1] if i + 1 < len(positions) else len(data)
        while end > start and data[end - 1] == 0:
            end -= 1
        nals.append(data[start:end])
    return nals


def _nal_type(nal: bytes) -> int:
    """Return the HEVC NAL unit type of a NAL unit (without start code)."""
    return (nal[0] >> 1) & 0x3F


def _build_seq_header_payload(frame_body: bytes) -> bytes | None:
    """Return an Annex-B parameter-set byte stream from a keyframe body."""
    param_sets = [
        nal
        for nal in _split_annexb(frame_body)
        if _nal_type(nal) in (NAL_VPS, NAL_SPS, NAL_PPS)
    ]
    if not param_sets:
        return None
    return b"".join(b"\x00\x00\x00\x01" + nal for nal in param_sets)


class FlvNormalizer:
    """Rebuild one continuous FLV stream from successive sessions."""

    def __init__(self) -> None:
        """Initialize the normalizer."""
        self._buf = bytearray()
        # Shared output clock: raw-paced from the vendor timestamps, anchored
        # on the wall clock, re-anchored at session starts and clock jumps.
        self._anchor_wall = 0.0
        self._anchor_raw = 0
        self._anchor_out = 0
        self._last_v = 0
        self._last_a = 0
        self._new_session = False
        self._seq_payload: bytes | None = None
        self._seq_emitted = False
        self._framing_warned = False
        # The G.711 -> AAC transcoder lives across sessions.
        self._transcoder: AacTranscoder | None = None
        self._transcoder_failed = False
        self._aac_seq_tag = b""
        self._aac_seq_emitted = False

    @property
    def seq_header(self) -> bytes:
        """Return the synthetic sequence headers as complete FLV tags.

        Empty until the first keyframe (video) and the first AAC packet
        (audio) have been seen. The bridge prepends these bytes to subscriber
        history so a viewer that joins between sessions still starts the
        container with both tracks' extradata.
        """
        out = b""
        if self._seq_payload is not None:
            out += _make_tag(
                TAG_VIDEO, 0,
                bytes([0x1C, VIDEO_SEQ_HEADER, 0, 0, 0]) + self._seq_payload,
            )
        return out + self._aac_seq_tag

    def reset(self) -> None:
        """Drop any partial tag left over from a truncated session."""
        self._buf.clear()
        self._new_session = True

    def feed(self, data: bytes) -> bytes:
        """Parse FLV bytes and return the normalized, continuous tags."""
        self._buf += data
        out = bytearray()
        while True:
            if len(self._buf) >= 3 and self._buf[:3] == b"FLV":
                if len(self._buf) < 13:
                    break
                del self._buf[:13]
                self._new_session = True
                continue
            if len(self._buf) < 11:
                break
            size = (self._buf[1] << 16) | (self._buf[2] << 8) | self._buf[3]
            total = _tag_total(self._buf[0], size)
            if len(self._buf) < total:
                break
            tag = bytes(self._buf[:total])
            del self._buf[:total]
            normalized = self._normalize_tag(tag)
            if normalized:
                out += normalized
        return bytes(out)

    def _map_ts(self, raw: int) -> int:
        """Map a raw vendor timestamp onto the shared output clock."""
        now = time.monotonic()
        if self._new_session or self._anchor_wall == 0.0:
            # Session start (or the very first tag): continue the output clock
            # after everything already emitted, on the wall clock.
            self._anchor_wall = now
            self._anchor_raw = raw
            self._anchor_out = max(self._last_v, self._last_a) + 1
            self._new_session = False
        else:
            step = raw - self._anchor_raw
            wall_target = self._anchor_out + int((now - self._anchor_wall) * 1000)
            candidate = self._anchor_out + step
            if step < 0 or step > MAX_RAW_STEP_MS or abs(candidate - wall_target) > DRIFT_LIMIT_MS:
                # Vendor clock jump, or raw rate drifted away from wall rate:
                # re-anchor on the wall clock so the output neither jumps with
                # the raw clock nor moves backwards.
                self._anchor_wall = now
                self._anchor_raw = raw
                self._anchor_out = max(
                    wall_target, self._last_v + 1, self._last_a + 1
                )
        return self._anchor_out + (raw - self._anchor_raw)

    def _normalize_tag(self, tag: bytes) -> bytes:
        """Return the rewritten form of one tag, or b"" to drop it."""
        tag_type = tag[0]
        if tag_type in (TAG_VRS_META, TAG_SCRIPT):
            return b""  # vendor liveTimestamp frames; vendor metadata
        if tag_type == TAG_AUDIO:
            return self._normalize_audio(tag)
        if tag_type != TAG_VIDEO:
            return b""  # anything unknown
        if len(tag) < 16:
            return b""  # not enough for a video tag header

        payload = tag[11:-4]
        flags = payload[0]
        packet_type = payload[1]
        if packet_type == VIDEO_SEQ_HEADER:
            # The vendor's own hvcC record. Dropping it is what forces the
            # MOV muxer to see Annex-B extradata (ours, below) and convert
            # samples; keeping it would queue it as new extradata and switch
            # the muxer back to passthrough mid-stream.
            return b""
        if packet_type != VIDEO_FRAMES:
            return b""  # end-of-sequence and anything unknown

        # Vendor frames are [4-byte total length][Annex-B]. Strip the length
        # prefix so the demuxer emits a clean Annex-B packet.
        head = payload[:5]
        body = payload[5:]
        if len(body) > 4 and body[:4] == (len(body) - 4).to_bytes(4, "big"):
            body = body[4:]
            tag = _make_tag(TAG_VIDEO, _tag_timestamp(tag), head + body)
        elif not self._framing_warned:
            self._framing_warned = True
            _LOGGER.warning(
                "Blurams live frame does not match the expected "
                "[length][Annex-B] framing; passing it through unchanged"
            )

        if (flags >> 4) == 1 and self._seq_payload is None:
            seq_payload = _build_seq_header_payload(body)
            if seq_payload is not None:
                self._seq_payload = seq_payload
                self._seq_emitted = False

        out = max(self._map_ts(_tag_timestamp(tag)), self._last_v + 1)
        self._last_v = out

        frame = _with_timestamp(tag, out)
        if self._seq_payload is not None and not self._seq_emitted:
            # Emit the synthetic sequence header immediately before the first
            # keyframe, at the keyframe's output timestamp (the demuxer turns
            # packet-type-0 video tags into extradata, so the timestamp is
            # cosmetic; keeping it equal avoids a backwards clock at start).
            self._seq_emitted = True
            seq_tag = _make_tag(
                TAG_VIDEO, out,
                bytes([0x1C, VIDEO_SEQ_HEADER, 0, 0, 0]) + self._seq_payload,
            )
            return seq_tag + frame
        return frame

    def _normalize_audio(self, tag: bytes) -> bytes:
        """Return an AAC form of an audio tag, or b"" when unsupported."""
        payload = tag[11:-4]
        if len(payload) < 1:
            return b""
        fmt = payload[0] >> 4

        if fmt == SOUND_FORMAT_AAC:
            # A camera model already sending AAC: pass it through with a
            # remapped timestamp and remember its sequence header so
            # mid-stream viewers start the container with extradata.
            packet_type = payload[1] if len(payload) > 1 else AAC_RAW
            out = max(self._map_ts(_tag_timestamp(tag)), self._last_a + 1)
            self._last_a = out
            if packet_type == AAC_SEQ_HEADER:
                self._aac_seq_tag = _make_tag(TAG_AUDIO, 0, payload)
            return _with_timestamp(tag, out)

        if self._transcoder_failed or fmt not in (SOUND_FORMAT_ALAW, SOUND_FORMAT_MULAW):
            return b""  # unsupported sound format
        if len(payload) < 2:
            return b""

        if self._transcoder is None:
            try:
                self._transcoder = AacTranscoder()
            except Exception as err:
                self._transcoder_failed = True
                _LOGGER.warning(
                    "Blurams live audio unavailable: AAC encoder failed to "
                    "open (%s)", err,
                )
                return b""

        pcm = (
            alaw_to_pcm(payload[1:])
            if fmt == SOUND_FORMAT_ALAW
            else mulaw_to_pcm(payload[1:])
        )
        packets = self._transcoder.feed(pcm)
        if not packets:
            return b""  # encoder still filling its 1024-sample buffer

        out = max(self._map_ts(_tag_timestamp(tag)), self._last_a + 1)
        self._last_a = out

        if not self._aac_seq_tag:
            self._aac_seq_tag = _make_tag(
                TAG_AUDIO, 0,
                bytes([AAC_TAG_HEADER, AAC_SEQ_HEADER])
                + self._transcoder.audio_specific_config,
            )
        body = bytearray()
        if not self._aac_seq_emitted:
            # The sequence header must precede the first AAC frame; the
            # demuxer consumes it as extradata, so any valid timestamp works.
            self._aac_seq_emitted = True
            body += _make_tag(
                TAG_AUDIO, out,
                bytes([AAC_TAG_HEADER, AAC_SEQ_HEADER])
                + self._transcoder.audio_specific_config,
            )
        for packet in packets:
            body += _make_tag(
                TAG_AUDIO, out, bytes([AAC_TAG_HEADER, AAC_RAW]) + packet
            )
        return bytes(body)


def find_keyframe_offset(data: bytes) -> int | None:
    """Return the offset of the last complete AVC keyframe tag, if any.

    Used to trim subscriber history so every new viewer starts on a keyframe.
    Sequence headers (packet type 0) do not count. Trailing partial tags and
    non-video data yield None.
    """
    offset = 0
    length = len(data)
    if length >= 13 and data[:3] == b"FLV":
        offset = 13
    keyframe = None
    while offset + 11 <= length:
        size = (data[offset + 1] << 16) | (data[offset + 2] << 8) | data[offset + 3]
        total = _tag_total(data[offset], size)
        if size < 2 or offset + total > length:
            return keyframe
        if (
            data[offset] == TAG_VIDEO
            and (data[offset + 11] >> 4) == 1
            and data[offset + 12] == 1
        ):
            keyframe = offset
        offset += total
    return keyframe
