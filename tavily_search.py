#!/usr/bin/env python3
"""tavily_search.py "query" [-n 6] [--depth basic|advanced] [--answer] [--timeout 30]
Key: $TAVILY_API_KEY or $AGENT_SEARCH_KEY_DIR/tavily_key"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import emit, fail, key, post_json  # noqa: E402

a = argparse.ArgumentParser(description=__doc__)
a.add_argument("q")
a.add_argument("-n", type=int, default=6)
a.add_argument("--depth", default="advanced")
a.add_argument("--answer", action="store_true")
a.add_argument("--timeout", type=int, default=30)
a = a.parse_args()
t0 = time.time()
try:
    r = post_json("https://api.tavily.com/search", {"query": a.q, "max_results": a.n, "search_depth": a.depth,
                                                    "include_answer": a.answer},
                  {"Authorization": "Bearer " + key("TAVILY_API_KEY", "tavily_key")}, a.timeout)
    emit({"channel": "tavily", "ok": True, "answer": r.get("answer"), "results": [{"title": x.get("title"), "url": x.get("url"),
          "snippet": (x.get("content") or "")[:800]} for x in r.get("results", [])]}, t0)
except Exception as e:
    fail(e, t0, "tavily")
