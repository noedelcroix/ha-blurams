"""Blurams live video via the h5player WebSocket (FLV over WS).

The Blurams web player streams live video through a vendor WebSocket:

    wss://<vrs host>/h5player/live   (Origin: https://client.blurams.com)

Wire format (observed in browser captures):

- First message, TEXT: ``__reqJSONStr=<urlencoded base64 ciphertext>`` —
  AES-256-CBC with the vendor web-SDK key ``viWebsdkCrypto`` (zero-padded to
  32 bytes), zero IV, PKCS#7, base64. Note this is a *different* key from the
  one the JSON API uses (the API key is the site hostname).
- Client → server, BINARY (once, after the first FLV frame arrives):
  ``0x03`` + urlencoded JSON ``{"time": <ms since request>, "cmd": 3}``.
- Server → client, BINARY, first byte is a command:
  ``0x00`` — the remaining bytes are raw FLV stream data,
  ``0x04`` — the remaining bytes are urlencoded JSON control responses,
  anything else — ignored.

The stream is HEVC (H.265) FLV; the camera's audio track is G.711, which
browsers cannot take inside fMP4, so audio tags are transcoded to AAC
downstream (see ``flv.py`` and ``audio.py``). The server caps a live session
after roughly a minute and closes the socket; ``flv_proxy.py`` reconnects and
stitches the sessions back together.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import string
import time
from dataclasses import dataclass
from urllib.parse import quote

from aiohttp import ClientSession, ClientWSTimeout, WSMsgType
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from .const import LIVE_ORIGIN, LIVE_SDK_KEY

_LOGGER = logging.getLogger(__name__)

WS_PATH = "/h5player/live"
WS_HEARTBEAT = 20
# Close the session if the server goes quiet: normally FLV frames arrive
# constantly, so silence means a stalled camera or relay and the caller should
# reconnect.
SOCK_READ_TIMEOUT = 30
WS_CLOSE_TIMEOUT = 10
# A degraded session keeps its socket alive by sending 0x04 control frames
# every ~4 s while forwarding no video at all (observed when the relay stops
# serving the device). Healthy sessions never gap more than ~1.2 s between
# FLV messages, so this cutoff reliably detects a live-but-videoless socket;
# without it the receive loop would spin on control frames forever.
NO_FLV_TIMEOUT = 10

CMD_FLV = 0x00
CMD_REQ_MESSAGE = 0x03
CMD_CONTROL = 0x04

# Defaults observed in the web player's request. productKey identifies the
# camera family and comes from the cloud with the rest of the live parameters;
# the constants are only fallbacks.
DEFAULT_PRODUCT_KEY = "1cad3c83-d87"
DEFAULT_CHANNEL = "720p"


@dataclass(frozen=True)
class LiveParams:
    """Everything needed to open one h5player live session."""

    vrs_host: str
    token: str
    device_id: str
    product_key: str = DEFAULT_PRODUCT_KEY
    relay_server: str = ""
    channel: str = DEFAULT_CHANNEL
    has_audio: bool = False
    # The vendor serves the player over TLS; tests and offline playback can
    # override this to plain "ws".
    scheme: str = "wss"


class BluramsLiveError(Exception):
    """Raised when the live WebSocket session cannot be established or dies."""


def _sdk_encrypt(text: str) -> str:
    """Encrypt a live request payload with the vendor web-SDK key."""
    padder = PKCS7(128).padder()
    padded = padder.update(text.encode()) + padder.finalize()
    encryptor = Cipher(
        algorithms.AES(LIVE_SDK_KEY), modes.CBC(b"\0" * 16)
    ).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode()


def build_live_request(params: LiveParams) -> str:
    """Build the first TEXT message sent on the live WebSocket."""
    client_id = "WEBCLIENT_H5_" + "".join(
        random.choice(string.ascii_lowercase + string.digits) for _ in range(19)
    )
    request = {
        "requestTime": str(time.time_ns() // 1_000_000),
        "productKey": params.product_key,
        "deviceId": params.device_id,
        "channelNo": "",
        "token": params.token,
        "hasAudio": "true" if params.has_audio else "false",
        "region": "",
        "isPermanentStorage": "false",
        "channel": params.channel,
        "deviceName": "",
        "clientId": client_id,
        "shareId": "",
        "relayServer": params.relay_server,
        "isSDCardPlayback": "false",
        "preConnect": "false",
        "releaseVersion": "",
        "noAAC": "1",
    }
    blob = _sdk_encrypt(json.dumps(request, separators=(",", ":")))
    return "__reqJSONStr=" + quote(blob, safe="")


class BluramsLiveClient:
    """Streams raw FLV bytes from one h5player live WebSocket session.

    A session dies when the server caps it (about a minute) or when the camera
    or network misbehaves; callers are expected to catch :class:`BluramsLiveError`
    and open a new session.
    """

    def __init__(self, session: ClientSession, params: LiveParams) -> None:
        """Initialize the live client."""
        self._session = session
        self._params = params

    async def stream(self):
        """Yield raw FLV chunks until the session ends."""
        url = f"{self._params.scheme}://{self._params.vrs_host}{WS_PATH}"
        timeout = ClientWSTimeout(ws_receive=SOCK_READ_TIMEOUT, ws_close=WS_CLOSE_TIMEOUT)
        ctx = self._session.ws_connect(
            url,
            origin=LIVE_ORIGIN,
            heartbeat=WS_HEARTBEAT,
            timeout=timeout,
        )
        ws = None
        try:
            # Bounds the TCP/TLS/upgrade phase; the session's own default
            # timeout is minutes, which would stall the reconnect loop.
            ws = await asyncio.wait_for(ctx.__aenter__(), SOCK_READ_TIMEOUT)
            try:
                await ws.send_str(build_live_request(self._params))
                started = time.monotonic()
                last_flv = started
                sync_sent = False
                async for msg in ws:
                    if msg.type is not WSMsgType.BINARY or not msg.data:
                        if msg.type in (
                            WSMsgType.CLOSE,
                            WSMsgType.CLOSING,
                            WSMsgType.CLOSED,
                            WSMsgType.ERROR,
                        ):
                            raise BluramsLiveError(f"live socket closed ({msg.type})")
                        continue
                    cmd, body = msg.data[0], msg.data[1:]
                    if cmd == CMD_FLV:
                        if not sync_sent:
                            await self._send_time_sync(
                                ws, int((time.monotonic() - started) * 1000)
                            )
                            sync_sent = True
                        last_flv = time.monotonic()
                        yield body
                    elif cmd == CMD_CONTROL:
                        if time.monotonic() - last_flv > NO_FLV_TIMEOUT:
                            raise BluramsLiveError(
                                "no video data for "
                                f"{time.monotonic() - last_flv:.0f}s "
                                "(socket alive, video stopped)"
                            )
                        _LOGGER.debug(
                            "Blurams live control response for %s: %.160s",
                            self._params.device_id,
                            body.decode("utf-8", "replace"),
                        )
            except asyncio.CancelledError:
                raise
            except BluramsLiveError:
                raise
            except Exception as err:
                raise BluramsLiveError(f"live session failed: {err}") from err
        finally:
            await self._teardown(ctx, ws)

    @staticmethod
    async def _teardown(ctx, ws) -> None:
        """Close the live socket, forcing the transport shut if it stalls.

        ``ws.close()`` waits for the peer's CLOSE reply, and a peer that keeps
        sending without ever reading can hold that handshake open indefinitely
        (aiohttp's ``ws_close`` timeout only covers the reply wait). Cancel the
        handshake when it outlives its grace period; the cancellation path
        force-closes the transport.
        """
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(), WS_CLOSE_TIMEOUT)
            except asyncio.TimeoutError:
                pass
        try:
            await asyncio.wait_for(ctx.__aexit__(None, None, None), WS_CLOSE_TIMEOUT)
        except asyncio.TimeoutError:
            pass

    @staticmethod
    async def _send_time_sync(ws, elapsed_ms: int) -> None:
        """Send the single time-sync frame, matching the web player's shape."""
        payload = quote(json.dumps({"time": elapsed_ms, "cmd": 3}))
        await ws.send_bytes(bytes([CMD_REQ_MESSAGE]) + payload.encode())
