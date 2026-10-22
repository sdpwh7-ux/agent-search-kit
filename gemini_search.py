#!/usr/bin/env python3
"""gemini_search.py "query" [--model M] [--thinking low|high] [--no-grounding] [--timeout 300]

Gemini streamGenerateContent with Google Search grounding. Returns the answer, the search
queries Gemini issued, and grounded sources.
Config: $GEMINI_API_KEY (or $AGENT_SEARCH_KEY_DIR/gemini_key), $GEMINI_BASE_URL
(default https://generativelanguage.googleapis.com; any compatible gateway works),
$GEMINI_SEARCH_MODEL (default gemini-flash-latest)."""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import emit, fail, key, sse, urls_in  # noqa: E402

a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
a.add_argument("q")
a.add_argument("--model", default=os.environ.get("GEMINI_SEARCH_MODEL", "gemini-flash-latest"))
a.add_argument("--thinking", default=None, help="thinkingLevel (models that support it), e.g. high")
a.add_argument("--timeout", type=int, default=300)
a.add_argument("--no-grounding", action="store_true")
a = a.parse_args()
t0 = time.time()
base = os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com").rstrip("/")
body = {"contents": [{"role": "user", "parts": [{"text": a.q + "\n\nSearch the web, give a concise conclusion and list source URLs."}]}]}
if a.thinking:
    body["generationConfig"] = {"thinkingConfig": {"thinkingLevel": a.thinking}}
if not a.no_grounding:
    body["tools"] = [{"google_search": {}}]
try:
    k = key("GEMINI_API_KEY", "gemini_key")
    text, srcs, queries = "", [], []
    for ev in sse(f"{base}/v1beta/models/{a.model}:streamGenerateContent?alt=sse", body, {"x-goog-api-key": k}, a.timeout):
        for c in ev.get("candidates", []):
            for p in (c.get("content") or {}).get("parts", []):
                if p.get("text") and not p.get("thought"):
                    text += p["text"]
            gm = c.get("groundingMetadata") or {}
            queries += gm.get("webSearchQueries", [])
            for ch in gm.get("groundingChunks", []):
                w = ch.get("web") or {}
                if w.get("uri"):
                    srcs.append({"title": w.get("title"), "url": w["uri"]})
    emit({"channel": "gemini", "model": a.model, "ok": bool(text), "grounded": bool(srcs or queries), "search_queries": queries,
          "answer": text, "sources": srcs or [{"url": u} for u in urls_in(text)]}, t0)
except Exception as e:
    fail(e, t0, "gemini")
