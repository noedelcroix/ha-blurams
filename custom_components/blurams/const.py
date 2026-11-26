"""Constants for the Blurams integration."""

from datetime import timedelta

DOMAIN = "blurams"
API_URL = "https://client.blurams.com/api"
WEB_URL = "https://client.blurams.com"
HOSTNAME = "client.blurams.com"
CONF_BLURAMS_COOKIES = "blurams_cookies"

# Live video. The web player streams through a vendor WebSocket that carries
# FLV data; see live.py and docs/api.md. The SDK key below is the vendor web
# player's own obfuscation key (distinct from the site hostname used for the
# JSON API). The bridge re-serves the stream as HTTP-FLV on loopback for the
# stream integration.
LIVE_ORIGIN = WEB_URL
LIVE_SDK_KEY = b"viWebsdkCrypto".ljust(32, b"\0")
LIVE_BRIDGE_HOST = "127.0.0.1"
LIVE_BRIDGE_PORT = 8765

# How often the Blurams cloud is polled for device state.
UPDATE_INTERVAL = timedelta(seconds=60)

# CAPTCHA SDK (Aliyun Captcha 2.0) used on the browser sign-in page. Blurams
# switched from Cap (whose /api/captcha/ endpoints now redirect away) to
# Alibaba Cloud's captcha; the login page fetches its configuration from
# /api/aliyunCaptcha/config and initializes the SDK with region, prefix and
# SceneId from that response.
# The SDK is served from an unversioned URL, so it cannot be pinned like the
# previous Cap widget. To still enforce Subresource Integrity, put the hash of
# the current file in ALIYUN_SDK_SRI (see README) — but note that the hash must
# then be updated whenever Alibaba Cloud updates the file.
ALIYUN_SDK_URL = "https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js"
ALIYUN_SDK_SRI = ""
# Only scripts below this path are allowed by the sign-in page's
# Content-Security-Policy, instead of the whole internet. The SDK also pulls
# in helper scripts and stylesheets from the g. and x. subdomains.
ALIYUN_SDK_SOURCE = "https://*.alicdn.com"
# The widget talks to the Aliyun Captcha API of the configured region, e.g.
# https://<prefix>.captcha-open-southeast.aliyuncs.com for region "sgp".
ALIYUN_API_SOURCE = "https://*.aliyuncs.com"
