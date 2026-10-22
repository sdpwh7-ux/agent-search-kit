#!/usr/bin/env python3
"""jina_read.py URL [--max 20000] [--timeout 60] [--keyless] [--proxy URL]
-> JSON {ok, title, url, content(markdown), via} via r.jina.ai

Uses the key ($JINA_API_KEY or $AGENT_SEARCH_KEY_DIR/jina_key) when present. Without a key, or on
401/402/403/429 (e.g. balance exhausted), falls back to the free keyless reader (rate-limited per IP).
--proxy (or $AGENT_SEARCH_PROXY) sends the keyless request through an http/socks5h proxy via curl,
for hosts whose IP is blocked from anonymous use."""
import argparse
import json
import os
import sys
import time
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import UA, emit, fail, get_json, key, proxied_get  # noqa: E402

a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
a.add_argument("url")
a.add_argument("--max", type=int, default=20000)
a.add_argument("--timeout", type=int, default=60)
a.add_argument("--keyless", action="store_true", help="skip the key, use the free reader")
a.add_argument("--proxy", default=os.environ.get("AGENT_SEARCH_PROXY"))
a = a.parse_args()
t0 = time.time()
HDR = {"Accept": "application/json", "X-Return-Format": "markdown"}


def out(d, via):
    c = d.get("content") or ""
    emit({"channel": "jina_read", "ok": True, "via": via, "url": d.get("url"), "title": d.get("title"), "chars": len(c),
          "tokens": (d.get("usage") or {}).get("tokens"), "content": c[:a.max]}, t0)


def keyless():
    if a.proxy:
        code, body, err = proxied_get("https://r.jina.ai/" + a.url, a.proxy, dict(HDR, **{"User-Agent": UA}), a.timeout)
        if code != 200:
            raise RuntimeError(f"keyless HTTP {code or 'error'}: {(body or err)[:300]}")
        return out(json.loads(body).get("data", {}), "keyless-proxy")
    out(get_json("https://r.jina.ai/" + a.url, HDR, a.timeout).get("data", {}), "keyless")


try:
    k = None if a.keyless else key("JINA_API_KEY", "jina_key", required=False)
    if not k:
        keyless()
    else:
        try:
            out(get_json("https://r.jina.ai/" + a.url, dict(HDR, Authorization="Bearer " + k), a.timeout).get("data", {}), "key")
        except urllib.error.HTTPError as e:
            if e.code in (401, 402, 403, 429):
                keyless()
            else:
                raise
except Exception as e:
    fail(e, t0, "jina_read")
