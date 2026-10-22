#!/usr/bin/env python3
"""site_search.py DOMAIN "query" [-n 8] -> JSON results restricted to one site (e.g. reddit.com, news.ycombinator.com).

Tries Exa, then Tavily, each restricted to DOMAIN; whichever has a key and returns results wins.
Useful for sites that block scrapers or need a login to search."""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import emit, key, post_json, redact  # noqa: E402


def _exa(q, n, domain):
    x = post_json("https://api.exa.ai/search", {"query": q, "numResults": n, "includeDomains": [domain],
                  "contents": {"text": {"maxCharacters": 600}}}, {"x-api-key": key("EXA_API_KEY", "exa_key")}, 30)
    return [{"title": i.get("title"), "url": i.get("url"), "snippet": (i.get("text") or "")[:600]} for i in x.get("results", [])]


def _tavily(q, n, domain):
    x = post_json("https://api.tavily.com/search", {"query": q, "max_results": n, "include_domains": [domain]},
                  {"Authorization": "Bearer " + key("TAVILY_API_KEY", "tavily_key")}, 30)
    return [{"title": i.get("title"), "url": i.get("url"), "snippet": (i.get("content") or "")[:600]} for i in x.get("results", [])]


def main():
    a = argparse.ArgumentParser(description=__doc__)
    a.add_argument("domain")
    a.add_argument("q")
    a.add_argument("-n", type=int, default=8)
    a = a.parse_args()
    t0 = time.time()
    errs = {}
    for name, fn in (("exa", _exa), ("tavily", _tavily)):
        try:
            res = fn(a.q, a.n, a.domain)
            if res:
                emit({"channel": "site:" + a.domain, "ok": True, "via": name, "results": res}, t0)
                return
            errs[name] = "0 results"
        except Exception as e:
            errs[name] = redact(e)
    emit({"channel": "site:" + a.domain, "ok": False, "error": errs}, t0)
    sys.exit(1)


if __name__ == "__main__":
    main()
