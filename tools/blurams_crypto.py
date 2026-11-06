#!/usr/bin/env python3
"""Encrypt and decrypt Blurams web API payloads.

The Blurams web client wraps JSON bodies in AES-256-CBC (key derived from the
hostname, zero IV, PKCS#7 padding) and base64-encodes the result. This helper
reproduces that scheme so requests and responses can be inspected by hand.
See docs/api.md for the protocol notes.

Requires the ``cryptography`` package (already installed with Home Assistant)::

    pip install cryptography

Examples::

    # Decrypt a __reqJSONStr / __respJSONStr value copied from the browser
    python tools/blurams_crypto.py decrypt "r3ubZJIs..."

    # Encrypt a JSON document (argument, file via @path, or stdin)
    python tools/blurams_crypto.py encrypt '{"update": true}'
    python tools/blurams_crypto.py encrypt @payload.json
    echo '{"update": true}' | python tools/blurams_crypto.py encrypt -

    # Build a ready-to-send request body (prompts for the password)
    python tools/blurams_crypto.py login you@example.com
"""

from __future__ import annotations

import argparse
import base64
from getpass import getpass
import json
import sys
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

DEFAULT_HOSTNAME = "client.blurams.com"
_IV = b"\0" * 16


def derive_key(hostname: str = DEFAULT_HOSTNAME) -> bytes:
    """Return the AES-256 key: the hostname, truncated/zero-padded to 32 bytes."""
    return hostname.encode()[:32].ljust(32, b"\0")


def encrypt(data: Any, hostname: str = DEFAULT_HOSTNAME) -> str:
    """Encrypt a JSON-serializable object into a ``__reqJSONStr`` value."""
    plaintext = json.dumps(data, separators=(",", ":")).encode()
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(derive_key(hostname)), modes.CBC(_IV)).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()


def decrypt(value: str, hostname: str = DEFAULT_HOSTNAME) -> Any:
    """Decrypt a ``__reqJSONStr`` / ``__respJSONStr`` value into a JSON object."""
    decryptor = Cipher(algorithms.AES(derive_key(hostname)), modes.CBC(_IV)).decryptor()
    padded = decryptor.update(base64.b64decode(value)) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    return json.loads(unpadder.update(padded) + unpadder.finalize())


def _read_json(source: str) -> Any:
    """Read JSON from an argument, ``@file`` or ``-`` (stdin)."""
    if source == "-":
        return json.load(sys.stdin)
    if source.startswith("@"):
        with open(source[1:], encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(source)


def main() -> int:
    """Run the command line interface."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--hostname", default=DEFAULT_HOSTNAME, help="key source host")
    sub = parser.add_subparsers(dest="command", required=True)

    dec = sub.add_parser("decrypt", help="decrypt a base64 payload (or - for stdin)")
    dec.add_argument("value")

    enc = sub.add_parser("encrypt", help="encrypt JSON (text, @file or - for stdin)")
    enc.add_argument("json")

    login = sub.add_parser("login", help="build an encrypted login request body")
    login.add_argument("email")
    login.add_argument("--captcha-token", default="", help="captchaVerifyParam token")

    args = parser.parse_args()

    if args.command == "decrypt":
        value = sys.stdin.read().strip() if args.value == "-" else args.value
        print(json.dumps(decrypt(value, args.hostname), indent=2, ensure_ascii=False))
    elif args.command == "encrypt":
        print(encrypt(_read_json(args.json), args.hostname))
    else:
        payload: dict[str, Any] = {
            "email": args.email,
            "password": getpass("Blurams password: "),
            "captcha": "",
            "remberMe": False,
        }
        if args.captcha_token:
            payload["captchaVerifyParam"] = args.captcha_token
        print(json.dumps({"__reqJSONStr": encrypt(payload, args.hostname)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
