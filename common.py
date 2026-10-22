"""Shared helpers for the agent-search-kit CLIs (stdlib only).

Every CLI prints one JSON object to stdout: {"channel", "ok", ..., "latency_s"}; on error
{"channel", "ok": false, "error"} and exit code 1. Keys are read from env vars or key files and
are never printed (errors are redacted).

Optional usage accounting: if a module named `usage_log` (see agent-usage-ledger) is importable,
every HTTP call is recorded there. Nothing happens otherwise.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:  # optional usage accounting; never breaks callers
    import usage_log as _UL
except Exception:  # pragma: no cover
    _UL = None

UA = os.environ.get("AGENT_SEARCH_UA", "agent-search-kit/0.1")
KEY_DIR = os.path.expanduser(os.environ.get("AGENT_SEARCH_KEY_DIR", "~/.config/agent-search"))
_SECRETS = []


def key(env_var, file_name=None, required=True):
    """Return an API key from $env_var, else from $AGENT_SEARCH_KEY_DIR/<file_name>. Never printed."""
    k = os.environ.get(env_var, "").strip()
    if not k and file_name:
        p = os.path.join(KEY_DIR, file_name)
        try:
            with open(p) as f:
                k = f.read().strip()
        except OSError:
            k = ""
    if not k:
        if required:
            raise RuntimeError(f"missing API key: set ${env_var} or create {os.path.join(KEY_DIR, file_name or '?')}")
        return None
    if k not in _SECRETS:
        _SECRETS.append(k)
    return k


def redact(s):
    s = str(s)
    for k in _SECRETS:
        s = s.replace(k, "***")
    return re.sub(r"(sk-|Bearer\s+)?[A-Za-z0-9_\-]{30,}", "***", s)


def _ul_model(url, body):
    m = (body or {}).get("model") if isinstance(body, dict) else None
    if not m and "/models/" in url:
        m = url.split("/models/", 1)[1].split(":", 1)[0].split("?", 1)[0]
    return m


def _ul_endpoint(url):
    e = re.sub(r"^\w+://[^/]+", "", url).split("?", 1)[0]
    return re.sub(r"/models/[^/:]+", "/models/*", e)


def _tracker(url, body, stream=False):
    if not _UL:
        return None
    try:
        return _UL.Tracker(_UL.provider_from_url(url), _ul_model(url, body), _ul_endpoint(url), stream=stream)
    except Exception:
        return None


def post_json(url, body, headers, timeout=60):
    h = {"Content-Type": "application/json", "User-Agent": UA}
    h.update(headers)
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h)
    tr = _tracker(url, body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
        if tr:
            tr.feed(d)
            tr.done(ok=True, status=200)
        return d
    except urllib.error.HTTPError as e:
        if tr:
            tr.done(ok=False, status=e.code, error=f"HTTP {e.code}")
        raise
    except Exception as e:
        if tr:
            tr.done(ok=False, status=None, error=repr(e))
        raise


def get_json(url, headers, timeout=60):
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers)
    req = urllib.request.Request(url, headers=h)
    tr = _tracker(url, None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
        if tr:
            tr.done(ok=True, status=200)
        return d
    except urllib.error.HTTPError as e:
        if tr:
            tr.done(ok=False, status=e.code, error=f"HTTP {e.code}")
        raise


def sse(url, body, headers, timeout=300):
    """POST with stream:true; yield parsed SSE data events."""
    h = {"Content-Type": "application/json", "User-Agent": UA, "Accept": "text/event-stream"}
    h.update(headers)
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h)
    tr = _tracker(url, body, stream=True)
    st = {"ok": True, "status": None, "error": None}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            st["status"] = r.status
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                d = line[5:].strip()
                if d == "[DONE]":
                    break
                try:
                    ev = json.loads(d)
                except ValueError:
                    continue
                if tr:
                    tr.feed(ev)
                yield ev
    except urllib.error.HTTPError as e:
        st.update(ok=False, status=e.code, error=f"HTTP {e.code}")
        raise
    except GeneratorExit:
        raise
    except BaseException as e:
        st.update(ok=False, error=repr(e))
        raise
    finally:
        if tr:
            tr.done(ok=st["ok"], status=st["status"], error=st["error"])


def proxied_get(url, proxy, headers, timeout=30):
    """GET through an http(s)/socks5h proxy via curl. Headers go through a curl config on stdin,
    so they never appear in the process list."""
    cfg = "".join(f'header = "{k}: {v}"\n' for k, v in headers.items())
    p = subprocess.run(["curl", "-sS", "-L", "--compressed", "-m", str(timeout), "-x", proxy, "-K", "-",
                        "-w", "\n__HTTP__%{http_code}", url], input=cfg, capture_output=True, text=True)
    body, _, code = p.stdout.rpartition("\n__HTTP__")
    return int(code or 0), body, p.stderr.strip()


def urls_in(text):
    found = (u.rstrip(".,;:!?") for u in re.findall(r"https?://[^\s\)\]\"'<>，。]+", text or ""))
    return list(dict.fromkeys(found))


def emit(obj, t0):
    obj["latency_s"] = round(time.time() - t0, 1)
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def fail(e, t0, channel):
    msg = str(e)
    if isinstance(e, urllib.error.HTTPError):
        try:
            msg = f"HTTP {e.code}: " + e.read()[:300].decode("utf-8", "replace")
        except Exception:
            msg = f"HTTP {e.code}"
    emit({"channel": channel, "ok": False, "error": redact(msg)}, t0)
    sys.exit(1)


def collect_urls(o, out=None):
    """Recursively collect URLs from any SSE/response object: url/uri fields, annotations, and
    `sources`/`citations` given as list, dict or plain string."""
    out = [] if out is None else out
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, str):
                if k in ("url", "uri") and v.startswith("http"):
                    out.append(v)
                elif k in ("sources", "citations", "source", "references"):
                    out += urls_in(v)
            else:
                collect_urls(v, out)
    elif isinstance(o, list):
        for v in o:
            if isinstance(v, str):
                out += urls_in(v) if v.startswith("http") else []
            else:
                collect_urls(v, out)
    return out


def responses_search(channel, base_url, api_key, model, prompt, tools, timeout, t0, extra=None):
    """Run an OpenAI-compatible /responses call (stream:true) with hosted search tools and emit
    {answer, sources, searches}. Works with any endpoint implementing the Responses API."""
    body = {"model": model, "stream": True, "tools": tools, "input": prompt}
    body.update(extra or {})
    try:
        text, cites, final, nsearch = "", [], None, 0
        for ev in sse(base_url.rstrip("/") + "/responses", body, {"Authorization": "Bearer " + api_key}, timeout):
            t = ev.get("type", "")
            if t == "response.output_text.delta":
                text += ev.get("delta", "")
            elif t.endswith("search_call.completed"):
                nsearch += 1
            elif t in ("response.completed", "response.failed", "response.incomplete", "error"):
                final = ev
            if t != "response.output_text.delta" and t != "response.completed":
                collect_urls(ev, cites)
        if final and final.get("type") not in ("response.completed", "response.incomplete"):
            raise RuntimeError(json.dumps(final)[:400])
        if final:
            for o in final.get("response", {}).get("output", []):
                if not text:
                    for c in o.get("content", []) or []:
                        if c.get("type") == "output_text":
                            text += c.get("text", "")
                if o.get("type", "").endswith("search_call") and not nsearch:
                    nsearch = 1
            collect_urls(final.get("response", {}).get("output", []), cites)
        r = (final or {}).get("response", {}) or {}
        sources = list(dict.fromkeys(re.sub(r"[?&]utm_source=openai$", "", u) for u in cites + urls_in(text)))
        emit({"channel": channel, "model": model, "resp_model": r.get("model"), "reasoning": r.get("reasoning"),
              "ok": bool(text), "searches": nsearch, "answer": text, "sources": sources}, t0)
    except Exception as e:
        fail(e, t0, channel)
