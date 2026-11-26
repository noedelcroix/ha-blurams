"""G.711 to AAC transcoding for the Blurams live bridge.

The camera's live audio arrives as G.711 A-law (FLV sound format 7) at 8 kHz
mono. Browsers' MSE cannot take G.711 inside fMP4 and Home Assistant's stream
component only remuxes, so the bridge decodes G.711 to PCM itself and
re-encodes AAC-LC with PyAV (already a Home Assistant dependency). The AAC
packets are handed back as standard FLV audio tags (sound format 10) that the
stream component muxes into the HLS output unchanged.

The decode tables are the classic G.711 companding expansions; the module
keeps both A-law and mu-law since the format byte selects between them.
"""

from __future__ import annotations

import struct

import av

_SAMPLE_RATE = 8000
_BIT_RATE = 32_000

_QUANT_MASK = 0x0F
_SEG_MASK = 0x70
_SEG_SHIFT = 4
_SIGN_BIT = 0x80


def _build_alaw_table() -> tuple[bytes, ...]:
    """Return s16le PCM bytes for each A-law byte."""
    table = []
    for raw in range(256):
        value = raw ^ 0x55
        t = (value & _QUANT_MASK) << 4
        seg = (value & _SEG_MASK) >> _SEG_SHIFT
        if seg == 0:
            t += 8
        elif seg == 1:
            t += 0x108
        else:
            t += 0x108
            t <<= seg - 1
        linear = t if value & _SIGN_BIT else -t
        table.append(struct.pack("<h", linear))
    return tuple(table)


def _build_mulaw_table() -> tuple[bytes, ...]:
    """Return s16le PCM bytes for each mu-law byte."""
    table = []
    for raw in range(256):
        value = ~raw & 0xFF
        t = ((value & _QUANT_MASK) << 3) + 0x84
        t <<= (value & _SEG_MASK) >> _SEG_SHIFT
        linear = (0x84 - t) if value & _SIGN_BIT else (t - 0x84)
        table.append(struct.pack("<h", linear))
    return tuple(table)


ALAW_TO_PCM = _build_alaw_table()
MULAW_TO_PCM = _build_mulaw_table()


def alaw_to_pcm(data: bytes) -> bytes:
    """Decode G.711 A-law bytes to s16le PCM."""
    return b"".join(map(ALAW_TO_PCM.__getitem__, data))


def mulaw_to_pcm(data: bytes) -> bytes:
    """Decode G.711 mu-law bytes to s16le PCM."""
    return b"".join(map(MULAW_TO_PCM.__getitem__, data))


class AacTranscoder:
    """Buffer PCM and re-encode it as AAC-LC frames.

    The encoder works on 1024-sample frames (128 ms at 8 kHz), so a few
    hundred milliseconds of audio accumulate before the first packet comes
    out. ``audio_specific_config`` carries the AudioSpecificConfig the FLV
    sequence header needs and is available as soon as the encoder is open.
    """

    def __init__(
        self,
        sample_rate: int = _SAMPLE_RATE,
        channels: int = 1,
        bit_rate: int = _BIT_RATE,
    ) -> None:
        """Open the AAC encoder."""
        self._ctx = av.CodecContext.create("aac", "w")
        self._ctx.sample_rate = sample_rate
        # The native AAC encoder only accepts planar float samples; the s16
        # PCM it is fed is converted in feed().
        self._ctx.format = "fltp"
        self._ctx.layout = "mono" if channels == 1 else "stereo"
        self._ctx.bit_rate = bit_rate
        self._ctx.open()
        self._channels = channels
        self._frame_samples = self._ctx.frame_size or 1024
        self._pcm = bytearray()
        self._pts = 0
        self.audio_specific_config = bytes(self._ctx.extradata or b"")

    def feed(self, pcm: bytes) -> list[bytes]:
        """Take s16le PCM and return encoded AAC frames (raw, no ADTS)."""
        self._pcm += pcm
        frame_bytes = self._frame_samples * 2 * self._channels
        packets = []
        while len(self._pcm) >= frame_bytes:
            chunk = bytes(self._pcm[:frame_bytes])
            del self._pcm[:frame_bytes]
            samples = struct.unpack(f"<{self._frame_samples * self._channels}h", chunk)
            plane = struct.pack(f"<{len(samples)}f", *(s / 32768.0 for s in samples))
            frame = av.AudioFrame(
                format="fltp",
                layout="mono" if self._channels == 1 else "stereo",
                samples=self._frame_samples,
            )
            frame.sample_rate = self._ctx.sample_rate
            frame.planes[0].update(plane)
            frame.pts = self._pts
            self._pts += self._frame_samples
            for packet in self._ctx.encode(frame):
                packets.append(bytes(packet))
        return packets
