"""Plain HTTPS GETs shared by the forward-log reader, the live coin list and the market view.

Standard library only (urllib), so it honours HTTPS_PROXY and the CA bundle the
rest of the app trusts, and needs no extra dependency.
"""

from __future__ import annotations

import os
import re
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable


class NotFound(Exception):
    """The path does not exist (yet), e.g. no outcomes folder before the first horizon closes."""


HttpGet = Callable[[str, dict, float], bytes]


def ssl_context() -> ssl.SSLContext:
    # Same CA bundle the rest of the app trusts (requests_trust_env), if one is set.
    for var in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
        p = os.environ.get(var)
        if p and Path(p).is_file():
            return ssl.create_default_context(cafile=p)
    return ssl.create_default_context()


def get(url: str, headers: dict, timeout_s: float) -> bytes:
    """GET `url` and return the body; honours HTTPS_PROXY. 404 raises NotFound."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=ssl_context()) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound(redact(url)) from e
        raise


def redact(text: str) -> str:
    """Drop URL query strings (GitHub download URLs carry tokens)."""
    return re.sub(r"\?\S*", "", text)
