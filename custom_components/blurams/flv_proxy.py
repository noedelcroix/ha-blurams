"""Local HTTP-FLV bridge between the Blurams live WebSocket and Home Assistant.

Home Assistant's stream component can only open streams with ffmpeg-readable
sources, and the live video is only reachable through the vendor's WebSocket
handshake. This bridge keeps one upstream session per watched device and
re-serves its FLV over plain HTTP on loopback:

    http://127.0.0.1:<port>/live/<device id>.flv

``Camera.stream_source`` points at that URL, and the ``stream`` integration
serves it to the frontend as HLS.

The server caps a live session after roughly a minute, so the bridge simply
reconnects while anyone is watching; :class:`~.flv.FlvNormalizer` stitches the
sessions into one continuous stream. A connection that goes quiet without
closing is cut by the read timeout and reconnected like any other failure. The
bridge listens on ``127.0.0.1`` only, so no authentication is needed on the URL.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from aiohttp import ClientSession, web

from .flv import FLV_HEADER, FlvNormalizer, find_keyframe_offset
from .live import BluramsLiveClient, LiveParams

_LOGGER = logging.getLogger(__name__)

# The upstream session is expected to end after about a minute; reconnect
# immediately when it does. Anything else (camera offline, network down) is
# retried on this slower cadence.
RECONNECT_DELAY = 1.0
ERROR_RETRY_DELAY = 5.0
# A session that ends before it lasted this long probably died for a real
# reason (camera offline, network down) rather than the server's session cap;
# back off longer before reconnecting.
MIN_GOOD_SESSION = 10.0
HISTORY_LIMIT = 512 * 1024
SUBSCRIBER_QUEUE_LIMIT = 256


class _Subscriber:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            SUBSCRIBER_QUEUE_LIMIT
        )
        self.closed = False


class _DeviceStream:
    """One watched device: upstream pump, shared normalizer and subscribers."""

    def __init__(
        self,
        hass,
        session: ClientSession,
        params_provider: Callable[[str], Awaitable[LiveParams | None]],
        device_id: str,
    ) -> None:
        """Initialize the device stream."""
        self._hass = hass
        self._session = session
        self._params_provider = params_provider
        self.device_id = device_id
        self.subscribers: set[_Subscriber] = set()
        self.history = bytearray()
        self._normalizer = FlvNormalizer()
        self._task: asyncio.Task | None = None

    def subscribe(self) -> _Subscriber:
        """Register a viewer and start the upstream if needed."""
        subscriber = _Subscriber()
        subscriber.queue.put_nowait(
            FLV_HEADER + self._normalizer.seq_header + bytes(self.history)
        )
        self.subscribers.add(subscriber)
        self._ensure_task()
        return subscriber

    def unsubscribe(self, subscriber: _Subscriber) -> None:
        """Deregister a viewer; stop the upstream when nobody is left."""
        self.subscribers.discard(subscriber)
        if not self.subscribers and self._task is not None:
            self._task.cancel()

    def broadcast(self, chunk: bytes) -> None:
        """Normalize a chunk, remember it and hand it to every subscriber."""
        data = self._normalizer.feed(chunk)
        if not data:
            return
        self._remember(data)
        for subscriber in list(self.subscribers):
            if subscriber.closed:
                continue
            try:
                subscriber.queue.put_nowait(data)
            except asyncio.QueueFull:
                subscriber.closed = True
                try:
                    subscriber.queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    def _remember(self, data: bytes) -> None:
        """Keep recent tags from the last keyframe for late subscribers."""
        offset = find_keyframe_offset(data)
        if offset is not None:
            self.history = bytearray(data[offset:])
        else:
            self.history += data
        if len(self.history) > HISTORY_LIMIT:
            offset = find_keyframe_offset(bytes(self.history))
            self.history = (
                bytearray(self.history[offset:]) if offset else bytearray()
            )

    def _ensure_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = self._hass.async_create_background_task(
                self._run(), name=f"blurams_live_{self.device_id}"
            )

    async def _run(self) -> None:
        """Keep one upstream session alive while anyone is watching."""
        while self.subscribers:
            params = await self._params_provider(self.device_id)
            if params is None or not params.vrs_host or not params.token:
                _LOGGER.warning(
                    "Blurams live parameters unavailable for %s", self.device_id
                )
                await asyncio.sleep(ERROR_RETRY_DELAY)
                continue
            started = time.monotonic()
            got_data = False
            client = BluramsLiveClient(self._session, params)
            try:
                async for chunk in client.stream():
                    got_data = True
                    self.broadcast(chunk)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                if got_data:
                    _LOGGER.debug(
                        "Blurams live session for %s ended: %s", self.device_id, err
                    )
                else:
                    _LOGGER.warning(
                        "Blurams live session for %s could not start: %s",
                        self.device_id,
                        err,
                    )
            finally:
                self._normalizer.reset()
            if not self.subscribers:
                break
            if not got_data:
                await asyncio.sleep(ERROR_RETRY_DELAY)
                continue
            # A session that ends after a normal watch time is the server cap;
            # reconnect right away. One that dies much earlier gets a longer
            # pause so a hard-down camera does not spin the loop.
            await asyncio.sleep(
                RECONNECT_DELAY if time.monotonic() - started > MIN_GOOD_SESSION
                else ERROR_RETRY_DELAY
            )


class FlvProxy:
    """HTTP server re-serving the live streams as plain HTTP-FLV."""

    def __init__(
        self,
        hass,
        session: ClientSession,
        params_provider: Callable[[str], Awaitable[LiveParams | None]],
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        """Initialize the proxy."""
        self._hass = hass
        self._session = session
        self._params_provider = params_provider
        self.host = host
        self.port = port
        self._streams: dict[str, _DeviceStream] = {}
        self._runner: web.AppRunner | None = None

    def url_for(self, device_id: str) -> str:
        """Return the HTTP-FLV URL for a device."""
        return f"http://{self.host}:{self.port}/live/{device_id}.flv"

    def _stream(self, device_id: str) -> _DeviceStream:
        stream = self._streams.get(device_id)
        if stream is None:
            stream = _DeviceStream(
                self._hass, self._session, self._params_provider, device_id
            )
            self._streams[device_id] = stream
        return stream

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        device_id = request.match_info["device"]
        stream = self._stream(device_id)
        subscriber = stream.subscribe()
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "video/x-flv", "Cache-Control": "no-cache"},
        )
        await response.prepare(request)
        try:
            while True:
                chunk = await subscriber.queue.get()
                if chunk is None:
                    break
                await response.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            stream.unsubscribe(subscriber)
        return response

    async def start(self) -> None:
        """Start the HTTP server."""
        app = web.Application()
        app.router.add_get("/live/{device}.flv", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        _LOGGER.info("Blurams FLV bridge listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        """Stop the HTTP server and all upstream sessions."""
        for stream in self._streams.values():
            if stream._task is not None and not stream._task.done():
                stream._task.cancel()
        self._streams.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
