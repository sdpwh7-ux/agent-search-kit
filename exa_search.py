#!/usr/bin/env python3
"""exa_search.py "query" [-n 6] [--timeout 30]  -> JSON results[{title,url,date,snippet}]
Key: $EXA_API_KEY or $AGENT_SEARCH_KEY_DIR/exa_key"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import emit, fail, key, post_json  # noqa: E402

a = argparse.ArgumentParser(description=__doc__)
a.add_argument("q")
a.add_argument("-n", type=int, default=6)
a.add_argument("--timeout", type=int, default=30)
a = a.parse_args()
t0 = time.time()
try:
    r = post_json("https://api.exa.ai/search", {"query": a.q, "numResults": a.n, "contents": {"text": {"maxCharacters": 800}}},
                  {"x-api-key": key("EXA_API_KEY", "exa_key")}, a.timeout)
    emit({"channel": "exa", "ok": True, "results": [{"title": x.get("title"), "url": x.get("url"), "date": x.get("publishedDate"),
          "snippet": (x.get("text") or "")[:800]} for x in r.get("results", [])]}, t0)
except Exception as e:
    fail(e, t0, "exa")
