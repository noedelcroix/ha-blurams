# Blurams for Home Assistant

[![Validate](https://github.com/noedelcroix/ha-blurams/actions/workflows/validate.yml/badge.svg)](https://github.com/noedelcroix/ha-blurams/actions/workflows/validate.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.1%2B-blue.svg)](https://www.home-assistant.io)

Custom integration that connects **Blurams cloud cameras** to Home Assistant.

> **Disclaimer:** this is an unofficial, community project. It is not affiliated
> with, endorsed by, or supported by Blurams. It uses the same web API as the
> Blurams website, which may change without notice.

## Features

| Capability | Details |
| --- | --- |
| Camera snapshots | The latest thumbnail from the Blurams cloud |
| Live video | HEVC live stream relayed through the Blurams cloud into Home Assistant's stream integration (HLS in the frontend); no audio |
| Power switch | Turn each camera on or off |
| Automatic updates | State is polled from the Blurams cloud every 60 seconds |
| Auto-discovery | Cameras added to your Blurams account appear without reconfiguring |
| Re-authentication | Home Assistant prompts you when the Blurams session expires |

### Entities

For every camera on your account:

| Entity | Platform | Notes |
| --- | --- | --- |
| `<camera name>` | `camera` | Unavailable while the camera is offline; live video through the `stream` integration |
| `<camera name> Power` | `switch` | Stays available when the camera is switched off |

## Requirements

- Home Assistant **2025.1.0** or newer
- A Blurams account
- An **internal or external URL** configured in Home Assistant
  (*Settings > System > Network*) that the browser used for setup can reach.
  Sign-in happens on a page served by your Home Assistant, because Blurams
  requires an interactive CAPTCHA.

## Installation

### HACS (recommended)

1. In HACS, open the menu (⋮) and choose **Custom repositories**.
2. Add `https://github.com/noedelcroix/ha-blurams` with category **Integration**.
3. Search for **Blurams** in HACS and select **Download**.
4. Restart Home Assistant.

### Manual

1. Copy the `custom_components/blurams` folder into your Home Assistant
   `custom_components` directory.
2. Restart Home Assistant.

## Setup

1. Go to **Settings > Devices & services > Add integration** and select **Blurams**.
2. A new browser tab opens with a sign-in page (allow pop-ups if it is blocked).
3. Enter your Blurams email and password, complete the "I'm a human" check and
   select **Sign in**.
4. Return to Home Assistant. Your cameras are added automatically.

If Blurams invalidates your session, Home Assistant shows a **Re-authenticate**
prompt. Complete the same sign-in page again.

## Privacy and security

- Your **password is never stored**. It is used once to sign in; only the Blurams
  session cookies are kept in the Home Assistant config entry.
- Home Assistant communicates with `client.blurams.com`. The sign-in page also
  loads the CAPTCHA SDK (Aliyun Captcha) from `o.alicdn.com`, and the SDK talks
  to `*.aliyuncs.com`.
- The sign-in pages are temporary: each is valid for 10 minutes, tied to a
  single setup flow, and allows a limited number of attempts.

## Troubleshooting

| Problem | Solution |
| --- | --- |
| "Home Assistant has no URL your browser can reach" | Set the internal or external URL under *Settings > System > Network*. |
| The human check never appears or fails | Make sure your browser can reach `o.alicdn.com` and `*.aliyuncs.com`, then retry. Check the browser console for blocked requests. |
| Camera shows as unavailable | The camera is offline in the Blurams cloud (for example after being switched off). |
| Live stream does not play | Make sure the `stream` integration is enabled (it is by default on standard installs) and that nothing else on the host uses port 8765. Check the logs for `blurams` errors. |
| Live stream hiccups about once a minute | Expected: the Blurams cloud caps each playback session; the integration reconnects automatically. |
| Entities stopped updating | Re-authenticate when prompted, or check *Settings > System > Logs* for `blurams` errors. |

To enable debug logs, add this to `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.blurams: debug
```

## Known limitations

- Live video is video-only: the cameras' G.711 audio track cannot be muxed by
  Home Assistant's stream component.
- The cameras stream HEVC (H.265). Home Assistant's HLS player plays it in
  Safari and in browsers with HEVC support (Chrome/Edge with hardware HEVC
  decoding enabled); other browsers show a black picture or fall back badly.
- The live stream flows through the Blurams cloud relay (there is no local
  RTSP on these cameras) and the cloud caps each playback session after
  roughly a minute; the integration reconnects automatically, so a brief hiccup
  is possible about once a minute.
- While the stream bridge only listens on `127.0.0.1`, any process on the Home
  Assistant host can open the stream URL.
- Cloud polling: state changes can take up to a minute to appear.
- Depends on Blurams' unofficial web API, which can change at any time.

## Contributing

Bug reports and pull requests are welcome. Please open an
[issue](https://github.com/noedelcroix/ha-blurams/issues) first for larger changes.
Include your Home Assistant version and the relevant log lines (remove any personal data).

### Development notes

- Protocol notes are in [`docs/api.md`](docs/api.md) and
  [`docs/openapi.yaml`](docs/openapi.yaml), including the live video WebSocket
  (`h5player`). `tools/blurams_crypto.py` encrypts and decrypts API payloads
  for debugging (`pip install cryptography`); live WebSocket payloads use the
  separate `viWebsdkCrypto` key (see `live.py`).
- The CAPTCHA SDK is Aliyun Captcha 2.0; its URL is pinned in
  `custom_components/blurams/const.py` (`ALIYUN_SDK_URL`). Alibaba Cloud serves
  it from an unversioned URL, so Subresource Integrity cannot be enforced as
  safely as with a versioned file: if you add the hash of the current file to
  `ALIYUN_SDK_SRI`, sign-in will fail whenever Alibaba Cloud updates the file
  and the hash must be updated too.

  ```bash
  curl -s https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js | openssl dgst -sha384 -binary | openssl base64 -A
  # then: ALIYUN_SDK_SRI = "sha384-<output>"
  ```
- CI runs Hassfest, HACS validation and a Python syntax check on every push.
- Release tags (`vX.Y.Z`) must match `version` in `manifest.json`.
