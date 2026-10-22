#!/usr/bin/env python3
"""web_search.py "query" --model MODEL [--tools web_search,x_search] [--effort high] [--tier T] [--timeout 600]

Answer a question with a model that has hosted search tools, through any OpenAI-compatible
**Responses API** endpoint (OpenAI, xAI, or a gateway that proxies them). Streams, so long
reasoning does not hit proxy timeouts. Output: answer, sources (URLs from annotations and text),
number of searches, resp_model.

Config: $RESPONSES_BASE_URL (default $OPENAI_BASE_URL or https://api.openai.com/v1)
        $RESPONSES_API_KEY  (default $OPENAI_API_KEY, or $AGENT_SEARCH_KEY_DIR/responses_key)
        $WEB_SEARCH_MODEL   default model when --model is omitted
Examples:
  web_search.py "latest CPython release" --model gpt-5
  RESPONSES_BASE_URL=https://api.x.ai/v1 RESPONSES_API_KEY=... web_search.py "what are people saying about X" \\
      --model grok-4 --tools web_search,x_search"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import fail, key, responses_search  # noqa: E402

a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
a.add_argument("q")
a.add_argument("--model", default=os.environ.get("WEB_SEARCH_MODEL"))
a.add_argument("--tools", default="web_search", help="comma-separated hosted tools, e.g. web_search,x_search")
a.add_argument("--effort", default=None, help="reasoning.effort, e.g. low|medium|high")
a.add_argument("--tier", default=None, help="service_tier to request, e.g. flex|priority")
a.add_argument("--timeout", type=int, default=600)
a = a.parse_args()
t0 = time.time()
try:
    if not a.model:
        raise RuntimeError("--model is required (or set WEB_SEARCH_MODEL)")
    base = os.environ.get("RESPONSES_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    k = key("RESPONSES_API_KEY", "responses_key", required=False) or key("OPENAI_API_KEY", "openai_key")
except Exception as e:
    fail(e, t0, "web_search")
extra = {}
if a.effort:
    extra["reasoning"] = {"effort": a.effort}
if a.tier:
    extra["service_tier"] = a.tier
tools = [{"type": t.strip()} for t in a.tools.split(",") if t.strip()]
responses_search("web_search", base, k, a.model,
                 a.q + "\n\nSearch before answering. Be concise and list every source URL.", tools, a.timeout, t0, extra)
