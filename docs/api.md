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

1. **CAPTCHA.** Login requires an interactive human check, served by a
   [Cap](https://capjs.js.org) instance under `/api/captcha/`. The browser
   widget calls `challenge` and `redeem` below that path and finally yields a
   token. The server may attach an *instrumentation* challenge (a script the
   widget must run), so the widget must be **version 0.1.51 or newer**; older
   widgets fail with "Instrumentation failed".
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
  "captchaVerifyParam": "<token from the Cap widget>"
}
```

- `remberMe` is spelled that way by the server (sic).
- `captcha` is a legacy image-captcha field; it is sent empty when the Cap token
  is used.
- Captured browser logins also contain `"path": "/"`. This integration does not
  send it and signs in fine, so it appears to be optional (*unverified* for all
  account types).

## Endpoints used by the integration

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/login` | Sign in, returns session cookies |
| `POST` | `/device/list` | List the cameras on the account |
| `POST` | `/device/saveSetting` | Change a device setting (used for power) |
| `GET`/`POST` | `/captcha/{path}` | Cap CAPTCHA endpoints (`challenge`, `redeem`) |

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
| `onlineStatus` | `"online"` when the camera is reachable |
| `deviceStatus` | `"OffByManual"` when switched off by the user; anything else is treated as on |
| `thumbnailUrlList[].url` | Snapshot path, relative to `https://client.blurams.com` (may be absolute) |

Thumbnails are fetched with a normal `GET` using the session cookies. Do not send
the cookies to any host other than `client.blurams.com`.

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
- Live video and audio: how streaming is negotiated is not documented here.
- Rate limits and exact session lifetime.
