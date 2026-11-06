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
| Camera snapshots | The latest thumbnail from the Blurams cloud (no live video stream yet) |
| Power switch | Turn each camera on or off |
| Automatic updates | State is polled from the Blurams cloud every 60 seconds |
| Auto-discovery | Cameras added to your Blurams account appear without reconfiguring |
| Re-authentication | Home Assistant prompts you when the Blurams session expires |

### Entities

For every camera on your account:

| Entity | Platform | Notes |
| --- | --- | --- |
| `<camera name>` | `camera` | Unavailable while the camera is offline |
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
  loads the CAPTCHA widget from `cdn.jsdelivr.net` (a pinned version).
- The sign-in pages are temporary: each is valid for 10 minutes, tied to a
  single setup flow, and allows a limited number of attempts.

## Troubleshooting

| Problem | Solution |
| --- | --- |
| "Home Assistant has no URL your browser can reach" | Set the internal or external URL under *Settings > System > Network*. |
| The human check never appears or fails | Make sure your browser can reach `cdn.jsdelivr.net`, then retry. Check the browser console for blocked requests. |
| Camera shows as unavailable | The camera is offline in the Blurams cloud (for example after being switched off). |
| Entities stopped updating | Re-authenticate when prompted, or check *Settings > System > Logs* for `blurams` errors. |

To enable debug logs, add this to `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.blurams: debug
```

## Known limitations

- No live video stream or audio, only snapshots.
- Cloud polling: state changes can take up to a minute to appear.
- Depends on Blurams' unofficial web API, which can change at any time.

## Contributing

Bug reports and pull requests are welcome. Please open an
[issue](https://github.com/noedelcroix/ha-blurams/issues) first for larger changes.
Include your Home Assistant version and the relevant log lines (remove any personal data).

### Development notes

- Protocol notes are in [`docs/api.md`](docs/api.md) and
  [`docs/openapi.yaml`](docs/openapi.yaml). `tools/blurams_crypto.py` encrypts and
  decrypts API payloads for debugging (`pip install cryptography`).
- The CAPTCHA widget version is pinned in `custom_components/blurams/const.py`
  (`CAP_WIDGET_URL`, version 0.1.51 or newer is required). To enforce
  Subresource Integrity, put the hash of that exact file in `CAP_WIDGET_SRI`:

  ```bash
  curl -s https://cdn.jsdelivr.net/npm/@cap.js/widget@0.1.56 | openssl dgst -sha384 -binary | openssl base64 -A
  # then: CAP_WIDGET_SRI = "sha384-<output>"
  ```
- CI runs Hassfest, HACS validation and a Python syntax check on every push.
- Release tags (`vX.Y.Z`) must match `version` in `manifest.json`.
