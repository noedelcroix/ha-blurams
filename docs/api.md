# Blurams web API notes

Reverse-engineered notes on the cloud API used by the Blurams website
(`https://client.blurams.com`) and by this integration. This is **unofficial**
and based on observation; Blurams can change it at any time. Anything marked
*unverified* has not been confirmed.

A machine-readable description is in [`openapi.yaml`](openapi.yaml). The helper
[`tools/blurams_crypto.py`](../tools/blurams_crypto.py) encrypts and decrypts
payloads so you can inspect requests captured in a browser.

## Transport

- Base URL: `https://client.blurams.com/api`
- Authentication: session **cookies** returned by `POST /login`. There is no
  token header; send the cookies on every request.
- Typical headers: `X-Requested-With: XMLHttpRequest`.

## Payload encryption

Request and response bodies are JSON documents wrapped in a simple obfuscation
layer implemented in the web client:

| Item | Value |
| --- | --- |
| Cipher | AES-256-CBC |
| Key | the hostname `client.blurams.com`, truncated or zero-padded to 32 bytes |
| IV | 16 zero bytes |
| Padding | PKCS#7 |
| Encoding | base64 |

Because the key is the public hostname, this protects nothing; it only means
requests are not plain JSON. Treat all traffic as sensitive anyway.

```python
key = b"client.blurams.com".ljust(32, b"\0")   # AES-256
iv = b"\0" * 16
```

### Envelope

- **Request**: `{"__reqJSONStr": "<base64 ciphertext>"}`, sent as
  `application/json` (see `/device/saveSetting` for the exception).
- **Response**: usually `{"__respJSONStr": "<base64 ciphertext>"}`. Some
  responses are plain JSON, so a client should check for the key first.

Decrypted responses carry a `code` field. `0` or `200` (number or string) means
success; other values are failures, with a human-readable `msg`.

## Authentication flow

1. **CAPTCHA.** Login requires an interactive human check, currently served by
   **Aliyun Captcha 2.0** (Alibaba Cloud). The site previously used
   [Cap](https://capjs.js.org) under `/api/captcha/`, but those endpoints now
   redirect to `/login` and are dead. The login page:
   - fetches `GET /api/aliyunCaptcha/config`, e.g.
     `{"enabled":true,"configured":true,"region":"sgp","prefix":"1o8reb","sceneId":"5ugp4tzx"}`;
   - loads `https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js`
     and sets `window.AliyunCaptchaConfig = {region, prefix}`;
   - calls `initAliyunCaptcha({SceneId, language, mode: "embed", element, button,
     success, fail, getInstance, onError})`. The widget talks to
     `https://<prefix>.captcha-open-southeast[-b].aliyuncs.com` for region
     `sgp` and finally yields a `captchaVerifyParam`.
   If `enabled` is false the site falls back to a legacy image captcha at
   `GET /api/captcha?_=<random>`.
2. **Login.** `POST /login` with the encrypted payload below, including the
   token as `captchaVerifyParam`. On success the response sets the session
   cookies.
3. **Use the API** with those cookies. When they expire, calls fail with an
   auth error (HTTP 401/403, or a failure `code`/`msg` mentioning login or
   session) and the user must sign in again.

### Login payload (decrypted)

```json
{
  "email": "user@example.com",
  "password": "********",
  "captcha": "",
  "remberMe": false,
  "captchaVerifyParam": "<token from the human check>"
}
```

- `remberMe` is spelled that way by the server (sic).
- `captcha` is a legacy image-captcha field; it is sent empty when the human check
  is used.
- Captured browser logins also contain `"path": "/"`. This integration does not
  send it and signs in fine, so it appears to be optional (*unverified* for all
  account types).

## Endpoints used by the integration

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/login` | Sign in, returns session cookies |
| `GET` | `/aliyunCaptcha/config` | Aliyun Captcha 2.0 configuration (region, prefix, sceneId) |
| `POST` | `/device/list` | List the cameras on the account |
| `POST` | `/device/innerLive` | Live view page; its HTML embeds the stream token |
| `POST` | `/device/saveSetting` | Change a device setting (used for power) |
| `GET` | `/captcha` | Legacy image captcha (only used when Aliyun Captcha is disabled) |

### `POST /device/list`

Decrypted request: `{"update": true}`.

Decrypted response:

```json
{
  "code": 0,
  "data": {
    "devicelist": [
      {
        "deviceid": "…",
        "devicename": "Living room",
        "cameraModelId": "…",
        "serialNumber": "…",
        "onlineStatus": "online",
        "deviceStatus": "On",
        "thumbnailUrlList": [{ "url": "/…" }]
      }
    ]
  }
}
```

Only the fields above are used by the integration; devices contain more.

| Field | Meaning |
| --- | --- |
| `deviceid` | Unique device id (used as the Home Assistant unique id) |
| `devicename` | Name set in the Blurams app |
| `cameraModelId`, `serialNumber` | Model and serial number |
| `onlineStatus` | `"online"` or `"available"` when the camera is reachable |
| `deviceStatus` | `"OffByManual"` when switched off by the user; anything else is treated as on |
| `thumbnailUrlList[].url` | Snapshot path, relative to `https://client.blurams.com` (may be absolute) |

Thumbnails are fetched with a normal `GET` using the session cookies. Do not send
the cookies to any host other than `client.blurams.com`.

Devices also carry the fields needed for live video:

| Field | Meaning |
| --- | --- |
| `did`, `cloudDid` | Numeric device identifiers, sent to `/device/innerLive` |
| `modelId` | Model family code (e.g. `V0I_00_00`) |
| `productkey` | Product key (e.g. `1cad3c83-d87`), sent in the live request |
| `region` | Streaming host with a `/cds` suffix (e.g. `vrs-fra-aliyun.blurams.com/cds`) |

## Live video

Live video is H.265/HEVC (observed 1920x1080 at ~15 fps on an A31S model;
other models may differ) in FLV containers, relayed through a vendor WebSocket.
The web player (`h5player`) negotiates it in two steps.

### `POST /device/innerLive?gTime=<epoch ms>`

Sent as `application/x-www-form-urlencoded` with the session cookies and a
browser-like `Referer` (the live page itself):

```
deviceAddSource=&cloudDid=<cloudDid>&modelId=<modelId>&shareid=<shareid>&did=<did>&deviceId=<deviceid>&channelNo=null
Referer: https://client.blurams.com/device/live?deviceId=<deviceid>&did=<did>&shareid=&modelId=<modelId>&cloudDid=<cloudDid>
```

The response is an **HTML page** (the player) whose inline script embeds the
stream parameters as JavaScript variables:

```js
token = 'wz8w2207…';                  // one-time stream token
productKey = '1cad3c83-d87';
relayServer: '10.110.1.103:50921',
html5PlayerServer: 'https://vrs-fra-aliyun.blurams.com',
```

The token is tied to the web session and must be fetched again for a new
playback session.

### `wss://<html5player host>/h5player/live`

WebSocket, `Origin: https://client.blurams.com`, **no cookies** — the token in
the first message is the only credential. The first client message is TEXT:

```
__reqJSONStr=<urlencoded base64 ciphertext>
```

Encrypted like the API payloads but with a **different key**: the vendor web
SDK key `viWebsdkCrypto`, zero-padded to 32 bytes (zero IV, PKCS#7, base64).
The decrypted request:

```json
{
  "requestTime": "<epoch ms>",
  "productKey": "1cad3c83-d87",
  "deviceId": "<deviceid>",
  "channelNo": "",
  "token": "<token>",
  "hasAudio": "true",
  "region": "",
  "isPermanentStorage": "false",
  "channel": "720p",
  "deviceName": "",
  "clientId": "WEBCLIENT_H5_<random>",
  "shareId": "",
  "relayServer": "10.110.1.103:50921",
  "isSDCardPlayback": "false",
  "preConnect": "false",
  "releaseVersion": "",
  "noAAC": "1"
}
```

After that, both directions use BINARY frames whose first byte is a command:

| First byte | Direction | Meaning |
| --- | --- | --- |
| `0x00` | server → client | Raw FLV stream data |
| `0x03` | client → server | Time sync: `urlencode({"time": <ms since request>, "cmd": 3})`, sent once after the first FLV frame |
| `0x04` | server → client | Control response: urlencoded JSON (e.g. `{"cmd":1,"data":{"audioEncodeType":0,"videoEncodeType":1}}`) |

Inside the FLV stream, the server interleaves a proprietary metadata tag of
type `0x61` carrying `liveTimestamp` JSON; it is not part of the standard FLV
tag set and must be dropped. The audio track is G.711, which Home Assistant's
stream component cannot mux.

The server **caps a live session after roughly a minute** and closes the
socket. The web player simply reconnects (with a fresh `innerLive` token); a
player must stitch the sessions together, because each restarts with its own
FLV header and timestamp base.

The same vendor infrastructure also exposes a device-control WebSocket
(`wss://wsrelay.blurams.com:<port>`, advertised as `websocketServer` on the
`innerLive` page) — likely used by the web player for PTZ and talk-back. Not
explored here yet.

### `POST /device/saveSetting`

Unlike the others this call is sent as
`application/x-www-form-urlencoded`, with a browser-like `Referer`:

```
__reqJSONStr=<base64 ciphertext>&_csrf_header=
Referer: https://client.blurams.com/api/device/innerLive?gTime=<epoch ms>
```

Decrypted request to change power:

```json
{
  "deviceId": "<deviceid>",
  "path": "profile/general/status",
  "element": "<status>On</status>"
}
```

`element` is `<status>On</status>` to power the camera on and
`<status>OffByManual</status>` to switch it off. Success is `"success": true`
or a success `code`.

## Inspecting traffic

```bash
# Decrypt a __reqJSONStr / __respJSONStr copied from the browser dev tools
python tools/blurams_crypto.py decrypt "<base64>"

# Encrypt a payload
python tools/blurams_crypto.py encrypt '{"update": true}'
```

Never share decrypted captures that contain your email, password, cookies or
device identifiers.

## Unknowns

- Other settings reachable through `/device/saveSetting` (`path` values) and the
  rest of the device object.
- Live audio (G.711 talk-back / listen) and the device-control WebSocket
  (`websocketServer`) used for PTZ and talk-back.
- Rate limits and exact session lifetime.
