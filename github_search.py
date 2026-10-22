#!/usr/bin/env python3
"""github_search.py - GitHub search for research agents: rate-limited, cached, token-safe.

Subcommands:
  repos   "keywords" [--lang python] [--min-stars 100] [--pushed-after 2026-01-01|90d] [--sort stars|best|updated] [-n 10] [--topic t]
  similar owner/repo|URL [-n 10] [--min-stars 10] [--lang X]   find related projects (topics + language + description
                                                                keywords, several searches merged and scored, self excluded)
  readme  owner/repo|URL [--max 3000]                          first N chars of the README
  code    "keywords" [--lang go] [--repo owner/repo] [-n 10]   code search (requires auth; skipped otherwise)
  issues  "query" [-n 10]                                      issue/PR search
  rate                                                         current rate-limit budget
Common: --json (JSON output; default is text)  --no-cache  --cache-ttl 600 (s)  --proxy socks5h://host:port (default direct)
Legacy form: github_search.py "q" [--type repositories|issues|code] [-n 5]  -> repos/issues/code, JSON output.

Auth order: $GITHUB_TOKEN -> $GH_TOKEN -> `gh auth token` (read in-process; never printed or stored). Works
unauthenticated too (search ~10 req/min).
Throttling: cross-process file lock in $GITHUB_SEARCH_STATE (default ~/.cache/github_search): authenticated
search 2.2 s/req, unauthenticated 6.5 s/req, code search 6.5 s/req. 429/403 rate limits back off using
Retry-After / x-ratelimit-reset (max 3 retries, each wait <= 65 s).
Cache: $GITHUB_SEARCH_STATE/cache/ (sha1 of URL, default 10 min, pruned after 1 day).
Optional usage accounting: if `usage_log` (agent-usage-ledger) is importable, each real HTTP call is recorded.
"""
import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

try:  # optional usage accounting
    import usage_log as _UL
except Exception:  # pragma: no cover
    _UL = None

API = "https://api.github.com"
STATE = os.path.expanduser(os.environ.get("GITHUB_SEARCH_STATE") or "~/.cache/github_search")
CACHE_DIR = os.path.join(STATE, "cache")
RL_FILE = os.path.join(STATE, "ratelimit")
UA = "agent-search-kit/0.1"
_TOKEN = None; _TOKEN_SRC = None
OPTS = {"cache": True, "ttl": 600, "proxy": None}
STOP = set("""a an the and or of for to in on with by from into via your you our is are be as at it its this that these those
using use used based simple fast lightweight easy powerful open source opensource tool tools library framework app application
project implementation server client support supports written made build built new best awesome official unofficial
cli api sdk python golang go js javascript typescript rust java node plugin plugins""".split())


# ---------- auth ----------
def token():
    global _TOKEN, _TOKEN_SRC
    if _TOKEN_SRC is not None: return _TOKEN
    for v in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(v): _TOKEN, _TOKEN_SRC = os.environ[v].strip(), "env:" + v; return _TOKEN
    try:
        p = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        if p.returncode == 0 and p.stdout.strip(): _TOKEN, _TOKEN_SRC = p.stdout.strip(), "gh"; return _TOKEN
    except Exception: pass
    _TOKEN, _TOKEN_SRC = None, "none"; return None


def redact(s):
    s = str(s); t = _TOKEN
    if t: s = s.replace(t, "***")
    return re.sub(r"\b(gh[opsur]_|github_pat_)[A-Za-z0-9_]+", "***", s)[:400]


# ---------- rate limit / cache ----------
def throttle(bucket):
    authed = bool(token())
    gap = {"search": 2.2 if authed else 6.5, "code": 6.5, "core": 0.2 if authed else 1.0}[bucket]
    os.makedirs(STATE, exist_ok=True)
    with open(RL_FILE + ("" if bucket == "search" else "." + bucket), "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0); last = float((f.read() or "0").strip() or 0)
            wait = last + gap - time.time()
            if wait > 0: time.sleep(wait)
            f.seek(0); f.truncate(); f.write(str(time.time())); f.flush()
        finally: fcntl.flock(f, fcntl.LOCK_UN)


def cache_get(url, accept):
    if not OPTS["cache"]: return None
    p = os.path.join(CACHE_DIR, hashlib.sha1((url + "|" + accept).encode()).hexdigest() + ".json")
    try:
        if time.time() - os.path.getmtime(p) < OPTS["ttl"]:
            return json.load(open(p))
    except Exception: pass
    return None


def cache_put(url, accept, data):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        p = os.path.join(CACHE_DIR, hashlib.sha1((url + "|" + accept).encode()).hexdigest() + ".json")
        tmp = p + ".tmp%d" % os.getpid(); json.dump(data, open(tmp, "w"), ensure_ascii=False); os.replace(tmp, p)
        # prune old entries (>1 day)
        for fn in os.listdir(CACHE_DIR):
            fp = os.path.join(CACHE_DIR, fn)
            if time.time() - os.path.getmtime(fp) > 86400: os.remove(fp)
    except Exception: pass


STATS = {"http": 0, "cache_hits": 0, "rate": None}


def _fetch(url, accept, timeout):
    h = {"Accept": accept, "User-Agent": UA, "X-GitHub-Api-Version": "2022-11-28"}
    t = token()
    if t: h["Authorization"] = "Bearer " + t
    req = urllib.request.Request(url, headers=h)
    if OPTS["proxy"]:  # SOCKS via curl (no SOCKS in stdlib); headers go via a stdin config so the token never appears in argv
        cfg = "".join(f'header = "{k}: {v}"\n' for k, v in h.items())
        p = subprocess.run(["curl", "-sS", "-L", "--compressed", "-m", str(timeout), "-x", OPTS["proxy"], "-K", "-",
                            "-D", "/dev/stderr", "-w", "\n__HTTP__%{http_code}", url], input=cfg, capture_output=True, text=True)
        body, _, code = p.stdout.rpartition("\n__HTTP__")
        hdrs = {}
        for ln in p.stderr.splitlines():
            if ":" in ln: k, v = ln.split(":", 1); hdrs[k.strip().lower()] = v.strip()
        return int(code or 0), hdrs, body
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read().decode("utf-8", "replace")


def gh_get(path, params=None, bucket="core", accept="application/vnd.github+json", timeout=25, raw=False):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    c = cache_get(url, accept)
    if c is not None: STATS["cache_hits"] += 1; return c
    last_err = None
    for attempt in range(4):
        throttle(bucket); t0 = time.time()
        try:
            code, hdrs, body = _fetch(url, accept, timeout)
        except Exception as e:
            code, hdrs, body = 0, {}, ""; last_err = redact(repr(e))
        STATS["http"] += 1
        if hdrs.get("x-ratelimit-remaining"):
            STATS["rate"] = {"bucket": hdrs.get("x-ratelimit-resource"), "limit": hdrs.get("x-ratelimit-limit"),
                             "remaining": hdrs.get("x-ratelimit-remaining"), "reset": hdrs.get("x-ratelimit-reset")}
        ok = code == 200
        if _UL:
            try: _UL.log_call("github", None, None, None, None, None, ok, code or None,
                              None if ok else (last_err or f"HTTP {code}: {redact(body[:200])}"), time.time() - t0,
                              re.sub(r"/repos/[^/]+/[^/]+", "/repos/*", path), auth=_TOKEN_SRC, retries=attempt)
            except Exception: pass
        if ok:
            data = body if raw else json.loads(body)
            cache_put(url, accept, data); return data
        limited = code == 429 or (code == 403 and ("rate limit" in body.lower() or hdrs.get("x-ratelimit-remaining") == "0"))
        if code and not limited and code < 500:
            raise RuntimeError(f"HTTP {code}: " + redact(body[:300]))
        if attempt == 3: break
        if limited:
            ra = hdrs.get("retry-after"); rs = hdrs.get("x-ratelimit-reset")
            wait = float(ra) if ra else (max(1.0, float(rs) - time.time() + 1) if rs and hdrs.get("x-ratelimit-remaining") == "0" else 10 * (attempt + 1))
            wait = min(wait, 65)
        else: wait = 2 * (attempt + 1)
        print(f"[github_search] HTTP {code or 'ERR'}, retrying in {wait:.0f}s ({attempt + 1}/3)", file=sys.stderr); time.sleep(wait)
        last_err = last_err or f"HTTP {code}"
    raise RuntimeError("request failed / rate limit did not recover: " + (last_err or "") + " " + redact(body[:200]))


# ---------- helpers ----------
def to_local(ts):
    if not ts: return None
    try: return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception: return ts


def parse_repo(s):
    s = s.strip().rstrip("/")
    m = re.search(r"github\.com[/:]([^/\s]+)/([^/\s#?]+)", s)
    o, r = (m.group(1), m.group(2)) if m else (s.split("/") + [""])[:2]
    r = re.sub(r"\.git$", "", r)
    if not o or not r: raise SystemExit("repository must be owner/repo or a GitHub URL")
    return o, r


def pushed_after(v):
    if not v: return None
    m = re.fullmatch(r"(\d+)([dmy])", v)
    if m:
        n, u = int(m.group(1)), m.group(2)
        return (datetime.now() - timedelta(days=n * {"d": 1, "m": 30, "y": 365}[u])).strftime("%Y-%m-%d")
    return v


def slim(i):
    lic = (i.get("license") or {}) or {}
    return {"full_name": i.get("full_name"), "url": i.get("html_url"), "stars": i.get("stargazers_count"),
            "forks": i.get("forks_count"), "pushed_at": to_local(i.get("pushed_at")), "language": i.get("language"),
            "license": lic.get("spdx_id") if lic.get("spdx_id") not in (None, "NOASSERTION") else (lic.get("name") or None),
            "description": i.get("description"), "topics": i.get("topics") or [], "archived": bool(i.get("archived")),
            "fork": bool(i.get("fork"))}


def build_q(q, lang=None, min_stars=None, pushed=None, topics=(), extra=""):
    parts = [q] if q else []
    parts += [f"topic:{t}" for t in topics]
    if lang: parts.append(f"language:{lang}")
    if min_stars: parts.append(f"stars:>={min_stars}")
    if pushed: parts.append(f"pushed:>={pushed}")
    if extra: parts.append(extra)
    return " ".join(parts)


def search_repos(q, sort, n):
    p = {"q": q, "per_page": min(max(n, 1), 100)}
    if sort == "stars": p.update(sort="stars", order="desc")
    elif sort == "updated": p.update(sort="updated", order="desc")
    d = gh_get("/search/repositories", p, bucket="search")
    return d.get("total_count"), [slim(i) for i in d.get("items", [])]


# ---------- commands ----------
def cmd_repos(a):
    q = build_q(a.query, a.lang, a.min_stars, pushed_after(a.pushed_after), a.topic or [],
                "" if a.include_archived else "archived:false")
    total, items = search_repos(q, a.sort, a.n)
    return {"mode": "repos", "q": q, "sort": a.sort, "total": total, "results": items[:a.n]}


GENERIC = set("""mcp mcp-server server ai llm llms agent agents ai-agent ai-agents automation bot chatgpt openai gpt claude
model models web data search python go golang typescript javascript rust tool cli api skill skills agent-skills claude-code
cursor docker self-hosted selfhosted open-source opensource hacktoberfest awesome deep research""".split())


def keywords(desc, name, topics=(), k=5):
    text = (name or "").replace("-", " ").replace("_", " ") + " " + " ".join(t.replace("-", " ") for t in topics) + " " + (desc or "")
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9\+\.]{2,}", text):
        w = re.sub(r"\.(com|cn|io|org|net|ai|dev)$", "", w.lower().strip(".-+"))
        if len(w) > 2 and w not in STOP and w not in out: out.append(w)
    spec = [w for w in out if w not in GENERIC]
    return spec[:k], [w for w in out if w in GENERIC][:3]


def cmd_similar(a):
    o, r = parse_repo(a.repo)
    me = gh_get(f"/repos/{o}/{r}")
    src = slim(me); full = (me.get("full_name") or f"{o}/{r}").lower()
    topics = [t for t in (me.get("topics") or []) if t not in ("hacktoberfest",)]
    lang = a.lang or me.get("language")
    spec, gen = keywords(me.get("description"), me.get("name") or r, topics)
    min_st = a.min_stars
    queries = []  # (label, q, weight)
    if spec:
        queries.append(("kw", build_q(" ".join(spec[:2]) + " in:name,description,topics", None, min_st), 3.0))
        if gen: queries.append(("kw+generic", build_q(" ".join(spec[:1] + gen[:1]) + " in:name,description,topics", None, min_st), 3.0))
        if lang: queries.append(("kw+lang", build_q(spec[0] + " in:name,description,topics", lang, min_st), 2.0))
    stopics = sorted(topics, key=lambda t: (t in GENERIC, -len(t)))  # specific topics first
    for t in stopics[:2]: queries.append((f"topic:{t}", build_q("", None, min_st, None, [t]), 1.0))
    if len(topics) >= 2: queries.append(("topics2", build_q("", None, min_st, None, stopics[:2]), 1.5))
    if not queries: queries.append(("name", build_q((me.get("name") or r).replace("-", " "), None, min_st), 1.0))
    queries = queries[:a.max_queries]
    pool, errs, ttot = {}, {}, {}
    for label, q, w in queries:
        try: total, items = search_repos(q + " archived:false", "best", 30)
        except Exception as e: errs[label] = str(e); continue
        if label.startswith("topic:"): ttot[label[6:]] = total or 0
        for rank, it in enumerate(items):
            fn = (it["full_name"] or "").lower()
            if fn == full or it["fork"]: continue
            e = pool.setdefault(fn, {**it, "_hits": [], "_rank": 0.0})
            e["_hits"].append(label); e["_rank"] += w * 3.0 / (rank + 3)
    def tw(t):  # topic specificity: rare topics count more
        if t in ttot: return 4.0 / math.log10(ttot[t] + 10)
        return 0.4 if t in GENERIC else 1.5
    res = []
    for e in pool.values():
        shared = sorted(set(topics) & set(e["topics"]))
        hay = ((e["description"] or "") + " " + e["full_name"] + " " + " ".join(e["topics"])).lower()
        kwhit = [k for k in spec if k in hay]
        score = sum(tw(t) for t in shared) + 4 * len(kwhit) + 0.3 * len([g for g in gen if g in hay]) + e["_rank"] \
            + 0.5 * math.log10((e["stars"] or 0) + 1) + (0.5 if lang and e["language"] == lang else 0)
        e.update(shared_topics=shared, matched_keywords=kwhit, matched_queries=e.pop("_hits"), score=round(score, 2)); e.pop("_rank")
        res.append(e)
    res.sort(key=lambda x: (-x["score"], -(x["stars"] or 0)))
    out = {"mode": "similar", "source": src, "signals": {"topics": topics, "language": lang, "keywords": spec, "generic": gen,
           "topic_totals": ttot}, "queries": [q for _, q, _ in queries], "results": res[:a.n]}
    if errs: out["errors"] = errs
    return out


def cmd_readme(a):
    o, r = parse_repo(a.repo)
    txt = gh_get(f"/repos/{o}/{r}/readme", accept="application/vnd.github.raw+json", raw=True)
    return {"mode": "readme", "repo": f"{o}/{r}", "chars": len(txt), "truncated": len(txt) > a.max, "content": txt[:a.max]}


def cmd_code(a):
    if not token():
        return {"mode": "code", "skipped": True, "reason": "code search requires GitHub auth (GITHUB_TOKEN/GH_TOKEN or gh auth login); skipped", "results": []}
    q = a.query + (f" language:{a.lang}" if a.lang else "") + (f" repo:{a.repo}" if a.repo else "")
    d = gh_get("/search/code", {"q": q, "per_page": min(a.n, 100)}, bucket="code",
               accept="application/vnd.github.text-match+json")
    res = [{"repo": i["repository"]["full_name"], "path": i.get("path"), "url": i.get("html_url"),
            "fragment": re.sub(r"\s+", " ", ((i.get("text_matches") or [{}])[0].get("fragment") or ""))[:240]}
           for i in d.get("items", [])]
    return {"mode": "code", "q": q, "total": d.get("total_count"), "results": res}


def cmd_issues(a):
    d = gh_get("/search/issues", {"q": a.query, "per_page": a.n}, bucket="search")
    return {"mode": "issues", "q": a.query, "total": d.get("total_count"),
            "results": [{"title": i.get("title"), "url": i.get("html_url"), "state": i.get("state"), "comments": i.get("comments"),
                         "updated_at": to_local(i.get("updated_at")), "snippet": re.sub(r"\s+", " ", i.get("body") or "")[:200]}
                        for i in d.get("items", [])]}


def cmd_rate(a):
    OPTS["cache"] = False
    d = gh_get("/rate_limit")["resources"]
    return {"mode": "rate", "resources": {k: {**d[k], "reset_at": datetime.fromtimestamp(d[k]["reset"]).strftime("%H:%M:%S")}
                                          for k in ("core", "search", "code_search") if k in d}}


# ---------- output ----------
def fmt_repo(i, k):
    flags = (" [archived]" if i.get("archived") else "")
    s = f"{k}. {i['full_name']}  ★{i['stars']}  fork {i['forks']}{flags}\n   {i['url']}\n" \
        f"   last push {i['pushed_at']} | {i.get('language') or '-'} | license {i.get('license') or 'none/unknown'}\n"
    if i.get("description"): s += f"   {i['description'][:200]}\n"
    if i.get("topics"): s += f"   topics: {', '.join(i['topics'][:10])}\n"
    if "score" in i: s += f"   similarity {i['score']} (shared topics: {', '.join(i['shared_topics']) or '-'}; keywords: {', '.join(i['matched_keywords']) or '-'})\n"
    return s


def print_text(o):
    m = o.get("mode")
    head = f"# GitHub {m}"
    if m == "repos": head += f" '{o['q']}': {o['total']} total, sorted by {o['sort']}"
    if m == "similar":
        s = o["source"]; head += f" <- {s['full_name']} ★{s['stars']}\nsignals: topics={o['signals']['topics']} language={o['signals']['language']} keywords={o['signals']['keywords']}"
    print(head + f"  ({o['latency_s']}s, auth={o['auth']}, http={o['http_calls']}, cache_hits={o['cache_hits']})\n")
    if m in ("repos", "similar"):
        for k, i in enumerate(o["results"], 1): print(fmt_repo(i, k))
    elif m == "readme":
        print(f"{o['repo']} README ({o['chars']} chars{', truncated' if o['truncated'] else ''})\n\n{o['content']}")
    elif m == "code":
        if o.get("skipped"): print("! " + o["reason"])
        for k, i in enumerate(o["results"], 1): print(f"{k}. {i['repo']}/{i['path']}\n   {i['url']}\n   {i['fragment']}\n")
    elif m == "issues":
        for k, i in enumerate(o["results"], 1): print(f"{k}. [{i['state']}] {i['title']}  comments {i['comments']}  {i['updated_at']}\n   {i['url']}\n")
    elif m == "rate":
        for k, v in o["resources"].items(): print(f"{k}: {v['remaining']}/{v['limit']}, resets at {v['reset_at']}")
    if o.get("errors"): print("errors:", o["errors"])
    if o.get("rate"): r = o["rate"]; print(f"\n[rate] {r.get('bucket')}: {r.get('remaining')}/{r.get('limit')} left")


def main():
    argv = sys.argv[1:]
    cmds = ("repos", "similar", "readme", "code", "issues", "rate")
    legacy = bool(argv) and argv[0] not in cmds and argv[0] not in ("-h", "--help")
    if legacy:  # legacy interface: github_search.py "q" [--type ...] [-n 5] -> JSON
        la = argparse.ArgumentParser(); la.add_argument("query"); la.add_argument("--type", default="repositories",
            choices=["repositories", "issues", "code"]); la.add_argument("-n", type=int, default=5); la.add_argument("--text", action="store_true")
        x, _ = la.parse_known_args(argv)
        argv = [{"repositories": "repos", "issues": "issues", "code": "code"}[x.type], x.query, "-n", str(x.n)] + ([] if x.text else ["--json"])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true"); common.add_argument("--no-cache", action="store_true")
    common.add_argument("--cache-ttl", type=int, default=600); common.add_argument("--proxy", default=None)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("repos", parents=[common]); p.add_argument("query"); p.add_argument("--lang"); p.add_argument("--min-stars", type=int)
    p.add_argument("--pushed-after", help="YYYY-MM-DD or 90d/6m/1y"); p.add_argument("--sort", choices=["stars", "best", "updated"], default="stars")
    p.add_argument("--topic", action="append"); p.add_argument("--include-archived", action="store_true"); p.add_argument("-n", type=int, default=10)
    p = sp.add_parser("similar", parents=[common]); p.add_argument("repo"); p.add_argument("-n", type=int, default=10)
    p.add_argument("--min-stars", type=int, default=10); p.add_argument("--lang"); p.add_argument("--max-queries", type=int, default=5)
    p = sp.add_parser("readme", parents=[common]); p.add_argument("repo"); p.add_argument("--max", type=int, default=3000)
    p = sp.add_parser("code", parents=[common]); p.add_argument("query"); p.add_argument("--lang"); p.add_argument("--repo"); p.add_argument("-n", type=int, default=10)
    p = sp.add_parser("issues", parents=[common]); p.add_argument("query"); p.add_argument("-n", type=int, default=10)
    sp.add_parser("rate", parents=[common])
    a = ap.parse_args(argv)
    OPTS.update(cache=not a.no_cache, ttl=a.cache_ttl, proxy=a.proxy)
    t0 = time.time(); token()
    try:
        out = globals()["cmd_" + a.cmd](a); out["ok"] = True
    except SystemExit: raise
    except Exception as e:
        out = {"mode": a.cmd, "ok": False, "error": redact(e)}
    out.update(channel="github", auth=_TOKEN_SRC, http_calls=STATS["http"], cache_hits=STATS["cache_hits"],
               rate=STATS["rate"], latency_s=round(time.time() - t0, 1))
    if a.json or not out["ok"]: print(json.dumps(out, ensure_ascii=False, indent=1))
    else: print_text(out)
    sys.exit(0 if out["ok"] else 1)


if __name__ == "__main__":
    main()
