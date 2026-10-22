"""Offline tests: helpers + every network CLI against a local fake server (no internet, no real keys)."""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import common  # noqa: E402
import github_search as G  # noqa: E402

FAKE_KEY = "fake-key-for-tests-only"


class Fake(http.server.BaseHTTPRequestHandler):
    seen = []
    protocol_version = "HTTP/1.0"

    def _sse(self, evs):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for e in evs:
            self.wfile.write(b"data: " + json.dumps(e).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")

    def _json(self, obj, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fake.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        if self.path == "/v1/responses":
            self._sse([{"type": "response.web_search_call.completed"},
                       {"type": "response.output_text.delta", "delta": "Answer: see https://example.org/a"},
                       {"type": "response.output_text.annotation.added", "annotation": {"url": "https://example.com/src"}},
                       {"type": "response.completed", "response": {"model": "fake-model", "output": []}}])
        elif ":streamGenerateContent" in self.path:
            self._sse([{"candidates": [{"content": {"parts": [{"text": "thinking", "thought": True}, {"text": "Gemini says hi"}]},
                                        "groundingMetadata": {"webSearchQueries": ["q1"],
                                                              "groundingChunks": [{"web": {"uri": "https://example.net/g", "title": "G"}}]}}]}])
        elif self.path == "/v1/tools/search":
            self._json({"results": [{"title": "T", "url": "https://example.com/d", "snippet": "s", "content": "c"}]})
        elif self.path == "/v1/tools/fetch":
            self._json({"title": "Page", "content": "a\\_b \\(c\\)"})
        else:
            self._json({"error": {"message": "nope " + FAKE_KEY}}, 401)

    def do_GET(self):
        self._json({"error": {"message": "nope " + FAKE_KEY}}, 401)

    def log_message(self, *a):
        pass


class OfflineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Fake.seen.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {k: v for k, v in os.environ.items() if not k.endswith(("_API_KEY", "_TOKEN", "_BASE_URL"))}
        self.env.update(AGENT_SEARCH_KEY_DIR=os.path.join(self.tmp.name, "keys"), AGENT_SEARCH_STATE=self.tmp.name,
                        GITHUB_SEARCH_STATE=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, script, *args, **env):
        r = subprocess.run([sys.executable, os.path.join(ROOT, script), *args], capture_output=True, text=True,
                           env=dict(self.env, **env), timeout=60)
        self.assertNotIn(FAKE_KEY, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        return r

    # ---- helpers
    def test_collect_urls(self):
        o = {"a": [{"url": "https://x.example/1"}, "https://y.example/2"], "sources": "see https://z.example/3, ok",
             "n": {"uri": "https://w.example/4"}}
        self.assertEqual(sorted(common.collect_urls(o)),
                         ["https://w.example/4", "https://x.example/1", "https://y.example/2", "https://z.example/3"])

    def test_key_from_file_and_redact(self):
        os.makedirs(self.env["AGENT_SEARCH_KEY_DIR"])
        with open(os.path.join(self.env["AGENT_SEARCH_KEY_DIR"], "exa_key"), "w") as f:
            f.write(FAKE_KEY + "\n")
        os.environ["AGENT_SEARCH_KEY_DIR"] = self.env["AGENT_SEARCH_KEY_DIR"]
        try:
            common.KEY_DIR = self.env["AGENT_SEARCH_KEY_DIR"]
            self.assertEqual(common.key("NO_SUCH_ENV_VAR_X", "exa_key"), FAKE_KEY)
            self.assertNotIn(FAKE_KEY, common.redact(f"error with {FAKE_KEY} inside"))
        finally:
            os.environ.pop("AGENT_SEARCH_KEY_DIR", None)

    def test_github_helpers(self):
        self.assertEqual(G.parse_repo("https://github.com/owner/repo.git"), ("owner", "repo"))
        self.assertEqual(G.parse_repo("owner/repo"), ("owner", "repo"))
        self.assertEqual(G.build_q("agent", "python", 10, "2026-01-01", ["llm"]),
                         "agent topic:llm language:python stars:>=10 pushed:>=2026-01-01")
        spec, gen = G.keywords("A background task ledger for AI agents", "taskwake", ["ai-agents", "webhook"])
        self.assertIn("taskwake", spec)
        self.assertIn("webhook", spec)
        self.assertRegex(G.pushed_after("30d"), r"^\d{4}-\d{2}-\d{2}$")

    # ---- CLIs against the fake server
    def test_missing_key_is_clean_json_error(self):
        r = self.cli("exa_search.py", "hello")
        self.assertEqual(r.returncode, 1)
        self.assertFalse(json.loads(r.stdout)["ok"])

    def test_web_search(self):
        r = self.cli("web_search.py", "q", "--model", "m", "--tools", "web_search,x_search", "--effort", "high",
                     RESPONSES_BASE_URL=self.base + "/v1", RESPONSES_API_KEY=FAKE_KEY)
        self.assertEqual(r.returncode, 0, r.stdout)
        d = json.loads(r.stdout)
        self.assertTrue(d["ok"])
        self.assertEqual(d["searches"], 1)
        self.assertEqual(d["resp_model"], "fake-model")
        self.assertEqual(set(d["sources"]), {"https://example.com/src", "https://example.org/a"})
        req = Fake.seen[-1]
        self.assertEqual(req["headers"]["authorization"], "Bearer " + FAKE_KEY)
        self.assertEqual(req["body"]["tools"], [{"type": "web_search"}, {"type": "x_search"}])
        self.assertEqual(req["body"]["reasoning"], {"effort": "high"})

    def test_gemini(self):
        r = self.cli("gemini_search.py", "q", "--model", "gm", GEMINI_BASE_URL=self.base, GEMINI_API_KEY=FAKE_KEY)
        d = json.loads(r.stdout)
        self.assertEqual((d["ok"], d["answer"], d["search_queries"]), (True, "Gemini says hi", ["q1"]))
        self.assertEqual(d["sources"][0]["url"], "https://example.net/g")
        self.assertEqual(Fake.seen[-1]["headers"]["x-goog-api-key"], FAKE_KEY)

    def test_deepseek(self):
        env = dict(DEEPSEEK_BASE_URL=self.base, DEEPSEEK_API_KEY=FAKE_KEY)
        d = json.loads(self.cli("deepseek_search.py", "search", "hello", "--json", **env).stdout)
        self.assertEqual(d["count"], 1)
        self.assertNotIn("content", d["results"][0])
        d = json.loads(self.cli("deepseek_search.py", "fetch", "https://example.com", "--json", **env).stdout)
        self.assertEqual(d["content"], "a_b (c)")
        r = self.cli("deepseek_search.py", "--balance", "--json", **env)
        self.assertEqual(r.returncode, 1)  # fake server returns 401 for GET; key must not leak
        self.assertIn("401", r.stdout)


if __name__ == "__main__":
    unittest.main()
