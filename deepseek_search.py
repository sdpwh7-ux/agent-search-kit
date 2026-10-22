#!/usr/bin/env python3
"""deepseek_search.py - client for DeepSeek's Search / Fetch tool API.

usage:
  deepseek_search.py search "query" [--n 10] [--content] [--ori "original question"] [--loc IP] [--json]
  deepseek_search.py fetch URL [--max 20000] [--raw] [--json]
  deepseek_search.py --balance [--json]

Endpoints (docs: https://api-docs.deepseek.com/guides/search):
  POST {base}/v1/tools/search  {query (1-128 chars), count 1-50, ori_question, localization, need_content}
  POST {base}/v1/tools/fetch   {url (<= 8192)} -> {title, content (markdown)}
  GET  {base}/user/balance
Billing is per successful request (failed/empty/timeout requests are not billed); the server
enforces per-account QPS. This client keeps <= 4 QPS per endpoint across all processes on the
machine (fcntl file lock) and retries only 429/5xx (max 2 retries, backoff).
Config: $DEEPSEEK_API_KEY (or $AGENT_SEARCH_KEY_DIR/deepseek_key), $DEEPSEEK_BASE_URL
(default https://api.deepseek.com), lock dir $AGENT_SEARCH_STATE (default ~/.cache/agent-search).
The key is never printed.
"""
import argparse
import fcntl
import json
import os
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import UA, key, redact  # noqa: E402

try:  # optional usage accounting
    import usage_log as _UL
except Exception:  # pragma: no cover
    _UL = None

BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
LOCK_DIR = os.path.expanduser(os.environ.get("AGENT_SEARCH_STATE") or "~/.cache/agent-search")
MIN_INTERVAL = 0.25          # 4 QPS per endpoint, shared by all processes on the machine
MAX_RETRIES = 2
ERR_HINT = {400: "invalid parameters", 401: "missing or invalid API key", 402: "insufficient balance",
            403: "fetch refused by safety policy", 404: "fetch could not extract usable content",
            408: "request body read timeout", 413: "request body too large", 429: "QPS limit exceeded",
            500: "server error", 504: "upstream timeout"}


class APIError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


MD_ESC = re.compile(r"\\([!-/:-@\[-`{-~])")


def unescape_md(c):
    """The Fetch API backslash-escapes almost all ASCII punctuation (even in code). Undo it."""
    return MD_ESC.sub(r"\1", c)


def rate_limit(bucket):
    """Cross-process limiter: flock a per-endpoint file holding the last slot time."""
    os.makedirs(LOCK_DIR, exist_ok=True)
    path = os.path.join(LOCK_DIR, f"deepseek_{bucket}.ratelimit")
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            try:
                last = float(f.read().strip() or 0)
            except ValueError:
                last = 0.0
            wait = last + MIN_INTERVAL - time.time()
            if wait > 0:
                time.sleep(wait)
            f.seek(0)
            f.truncate()
            f.write(f"{time.time():.6f}")
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _read_body(r):
    """Read the full body; tolerate SSE/chunked responses by merging data: lines."""
    txt = r.read().decode("utf-8", "replace")
    ctype = r.headers.get("Content-Type", "")
    if "event-stream" in ctype or txt.lstrip().startswith("data:"):
        parts = [ln[5:].strip() for ln in txt.splitlines() if ln.startswith("data:")]
        parts = [p for p in parts if p and p != "[DONE]"]
        if len(parts) == 1:
            return json.loads(parts[0])
        merged = {}
        for o in (json.loads(p) for p in parts):
            for k, v in o.items():
                if isinstance(v, list):
                    merged.setdefault(k, []).extend(v)
                elif isinstance(v, str) and isinstance(merged.get(k), str):
                    merged[k] += v
                else:
                    merged[k] = v
        return merged
    return json.loads(txt)


def request(method, path, body=None, bucket=None, timeout=60):
    """Same contract as _request_raw, plus one usage record per logical call (if usage_log is available)."""
    t0 = time.time()
    try:
        status, d, attempt = _request_raw(method, path, body, bucket, timeout)
    except APIError as e:
        if _UL:
            _UL.log_call("deepseek", path.rsplit("/", 1)[-1], None, None, None, None, False, e.code, e.msg, time.time() - t0, path)
        raise
    if _UL:
        billable = bool(isinstance(d, dict) and (d.get("results") or d.get("content"))) and path.startswith("/v1/tools/")
        _UL.log_call("deepseek", path.rsplit("/", 1)[-1], None, None, None, {"requests_billed": 1 if billable else 0},
                     True, status, None, time.time() - t0, path, retries=attempt or None)
    return status, d, attempt


def _request_raw(method, path, body=None, bucket=None, timeout=60):
    try:
        k = key("DEEPSEEK_API_KEY", "deepseek_key")
    except RuntimeError as e:
        raise APIError(0, str(e))
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    hdr = {"Authorization": "Bearer " + k, "User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        hdr["Content-Type"] = "application/json"
    attempt = 0
    while True:
        if bucket:
            rate_limit(bucket)
        req = urllib.request.Request(BASE + path, data=data, headers=hdr, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, _read_body(r), attempt
        except urllib.error.HTTPError as e:
            try:
                payload = e.read()[:2000].decode("utf-8", "replace")
            except Exception:
                payload = ""
            try:
                msg = json.loads(payload)["error"]["message"]
            except Exception:
                msg = payload[:300]
            if (e.code == 429 or 500 <= e.code < 600) and attempt < MAX_RETRIES:
                ra = e.headers.get("Retry-After") if e.headers else None
                try:
                    delay = float(ra)
                except (TypeError, ValueError):
                    delay = (1.5 * (3 ** attempt)) + random.uniform(0, 0.5)
                time.sleep(min(delay, 15))
                attempt += 1
                continue
            raise APIError(e.code, f"HTTP {e.code} {ERR_HINT.get(e.code, '')}: {redact(msg)}".strip())
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            raise APIError(-1, f"network error/timeout ({timeout}s): {redact(getattr(e, 'reason', e))}")
        except json.JSONDecodeError as e:
            raise APIError(-2, f"response is not valid JSON: {e}")


def out(obj, as_json, t0, text_fn):
    obj["latency_s"] = round(time.time() - t0, 2)
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=1))
    else:
        text_fn(obj)


def do_search(a):
    q = a.query.strip()
    if not q:
        raise APIError(400, "query must not be empty")
    if len(q) > 128:
        raise APIError(400, f"query longer than 128 chars ({len(q)}); shorten it")
    n = max(1, min(50, a.n))
    body = {"query": q, "count": n}
    if a.content:
        body["need_content"] = True
    if a.ori:
        body["ori_question"] = a.ori[:128]
    if a.loc:
        body["localization"] = a.loc
    t0 = time.time()
    status, d, retries = request("POST", "/v1/tools/search", body, "search", a.timeout)
    res = d.get("results") or []
    if not a.content:
        for r in res:
            r.pop("content", None)

    def txt(o):
        print(f"# DeepSeek search: {q}  ({len(res)} results, {o['latency_s']}s)")
        for i, r in enumerate(res, 1):
            print(f"\n{i}. {r.get('title')}\n   {r.get('url')}\n   [{r.get('site_name') or '-'} | {(r.get('date') or '-')[:10]} | "
                  f"score {r.get('rerank_score')} | auth {r.get('authority')}]\n   {(r.get('snippet') or '').strip()}")
    out({"channel": "deepseek_search", "ok": True, "query": q, "count": len(res), "billable": bool(res),
         "retries": retries, "results": res}, a.json, t0, txt)


def do_fetch(a):
    u = a.url.strip()
    if not re.match(r"^https?://[^/\s]+", u):
        raise APIError(400, "URL must be http(s) with a host")
    if len(u) > 8192:
        raise APIError(400, "URL longer than 8192 chars")
    t0 = time.time()
    status, d, retries = request("POST", "/v1/tools/fetch", {"url": u}, "fetch", a.timeout)
    c = d.get("content") or ""
    if not a.raw:
        c = unescape_md(c)

    def txt(o):
        print(f"# {o['title']}\n<!-- {u} | {o['chars']} chars | {o['latency_s']}s -->\n\n{o['content']}")
    out({"channel": "deepseek_fetch", "ok": bool(c), "url": u, "title": d.get("title", ""), "chars": len(c),
         "truncated": len(c) > a.max, "retries": retries, "content": c[:a.max]}, a.json, t0, txt)


def do_balance(a):
    t0 = time.time()
    status, d, _ = request("GET", "/user/balance", None, None, a.timeout)

    def txt(o):
        for b in o.get("balance_infos", []):
            print(f"{b.get('currency')} total={b.get('total_balance')} granted={b.get('granted_balance')} "
                  f"topped_up={b.get('topped_up_balance')} available={o.get('is_available')}")
    d.update({"channel": "deepseek_balance", "ok": True})
    out(d, a.json, t0, txt)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--balance", action="store_true", help="show account balance (GET /user/balance)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--timeout", type=int, default=60)
    sub = p.add_subparsers(dest="mode")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--n", type=int, default=10, help="1-50, default 10")
    s.add_argument("--content", action="store_true", help="need_content=true: include page text")
    s.add_argument("--ori", help="ori_question: the user's original question (<= 128 chars)")
    s.add_argument("--loc", help="localization IP")
    s.add_argument("--json", action="store_true")
    s.add_argument("--timeout", type=int, default=60)
    f = sub.add_parser("fetch")
    f.add_argument("url")
    f.add_argument("--max", type=int, default=20000, help="truncate output to N chars")
    f.add_argument("--raw", action="store_true", help="keep the API's markdown backslash escapes")
    f.add_argument("--json", action="store_true")
    f.add_argument("--timeout", type=int, default=90)
    a = p.parse_args()
    t0 = time.time()
    try:
        if a.balance:
            do_balance(a)
        elif a.mode == "search":
            do_search(a)
        elif a.mode == "fetch":
            do_fetch(a)
        else:
            p.print_help()
            sys.exit(2)
    except APIError as e:
        err = {"channel": f"deepseek_{a.mode or 'balance'}", "ok": False, "http_status": e.code, "error": e.msg,
               "latency_s": round(time.time() - t0, 2)}
        if getattr(a, "json", False):
            print(json.dumps(err, ensure_ascii=False, indent=1))
        else:
            print(f"ERROR: {e.msg}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
