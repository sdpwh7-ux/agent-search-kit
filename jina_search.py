#!/usr/bin/env python3
"""jina_search.py "query" [-n 5] [--max 1500] [--timeout 60] -> JSON results[{title,url,snippet}] via s.jina.ai
--max 0 returns no page content (faster, cheaper). Key: $JINA_API_KEY or $AGENT_SEARCH_KEY_DIR/jina_key"""
import argparse
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import emit, fail, get_json, key  # noqa: E402

a = argparse.ArgumentParser(description=__doc__)
a.add_argument("q")
a.add_argument("-n", type=int, default=5)
a.add_argument("--max", type=int, default=1500)
a.add_argument("--timeout", type=int, default=60)
a = a.parse_args()
t0 = time.time()
try:
    d = get_json("https://s.jina.ai/?q=" + urllib.parse.quote(a.q),
                 {"Authorization": "Bearer " + key("JINA_API_KEY", "jina_key"),
                  "X-Respond-With": "no-content" if a.max == 0 else "markdown"}, a.timeout).get("data", [])
    emit({"channel": "jina_search", "ok": True, "results": [{"title": x.get("title"), "url": x.get("url"),
          "snippet": (x.get("description") or "") + ("\n" + (x.get("content") or "")[:a.max] if a.max else "")}
          for x in d[:a.n]]}, t0)
except Exception as e:
    fail(e, t0, "jina_search")
