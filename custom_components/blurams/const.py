"""Constants for the Blurams integration."""

from datetime import timedelta

DOMAIN = "blurams"
API_URL = "https://client.blurams.com/api"
WEB_URL = "https://client.blurams.com"
HOSTNAME = "client.blurams.com"
CONF_BLURAMS_COOKIES = "blurams_cookies"

# How often the Blurams cloud is polled for device state.
UPDATE_INTERVAL = timedelta(seconds=60)

# CAPTCHA widget (Cap) used on the browser sign-in page. The version is pinned
# on purpose: the user types their password on that page, so it must never load
# whatever "latest" happens to be. To also enforce Subresource Integrity, put
# the "sha384-..." hash of that exact file in CAP_WIDGET_SRI (see README).
# The version must be >= 0.1.51: older widgets cannot run the instrumentation
# challenges that Blurams' CAPTCHA server sends ("Instrumentation failed").
CAP_WIDGET_URL = "https://cdn.jsdelivr.net/npm/@cap.js/widget@0.1.56"
CAP_WIDGET_SRI = ""
# Only scripts below this path (the widget and its WASM helper) are allowed by
# the sign-in page's Content-Security-Policy, instead of the whole CDN.
CAP_CDN_SOURCE = "https://cdn.jsdelivr.net/npm/@cap.js/"
