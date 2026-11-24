#!/usr/bin/env python3
"""A fake CPA for testing cpa_guard.py without touching a real account.

usage: fake_cpa.py CONFIG_JSON PORT
CONFIG_JSON: {"triggers": [substrings that cause a 429], "mode": "normal|tos",
              "patch": "replace|ignore|append|dropwords|timeout|getfail", "rewriter": "ok|echo|down"}
GET /dump returns the internal state (rules, calls) for the test scripts.
"""
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CFG = json.load(open(sys.argv[1]))
STATE = {"patterns": [{"pattern": "(?i)\\bCursor\\b", "replacement": "Antigravity"}], "words": True, "calls": []}
TRANSLATOR = "gpt-6-luna"
KEEP = re.compile(r"[`$\\/<>{}_]|[a-z][A-Z]")


def apply_rules(text):
    for p in STATE["patterns"]:
        try:
            text = re.sub(p["pattern"], p["replacement"], text)
        except re.error:
            pass
    return text


def fake_translate(prompt):
    """Replace every ordinary word with 词 (with 安 when asked for a softened rewrite); keep paths, code
    tokens and the listed tool names. A trigger of "词 词 词" makes plain translations still 429."""
    line = prompt.split("Line:\n", 1)[1].split("\n\n")[0]
    if CFG.get("rewriter") == "echo":
        return line
    m = re.search(r"tool names and must appear unchanged: ([^\n]+)", prompt)
    tools = set(m.group(1).split(", ")) if m else set()
    word = "安" if "content filter" in prompt else "词"
    return " ".join(w if KEEP.search(w) or w in tools else word for w in line.split())


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode())

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"null")

    def do_GET(self):
        STATE["calls"].append("GET " + self.path)
        if self.path == "/dump":
            return self.send(200, STATE)
        if self.path.endswith("/providers/antigravity"):
            if CFG.get("patch") == "getfail":
                return self.send(500, {})
            d = {"antigravity-credits": True, "sensitive-patterns-replace": STATE["patterns"]}
            if STATE["words"]:
                d["sensitive-words-replace"] = {"Codex": "Antigravity"}
            return self.send(200, d)
        if self.path == "/v8/management/credentials":
            return self.send(200, {"files": [{"provider": "antigravity", "auth_index": "a1"}]})
        self.send(404, {})

    def do_PATCH(self):
        d = self.body()
        STATE["calls"].append("PATCH " + self.path)
        mode, new = CFG.get("patch"), d.get("sensitive-patterns-replace")
        if new is not None:
            if mode == "append":
                STATE["patterns"] = STATE["patterns"] + new
            elif mode == "dropwords":
                STATE["patterns"], STATE["words"] = new, "sensitive-words-replace" in d
            elif mode == "timeout":
                STATE["patterns"] = new
                return self.send(500, {})
            elif mode != "ignore":
                STATE["patterns"] = new
        self.send(200, {"status": "ok"})

    def do_POST(self):
        d = self.body()
        if "cooldown/reset" in self.path:
            return self.send(200, {})
        if d.get("model") == TRANSLATOR or not any(m["role"] == "system" for m in d["messages"]):
            if CFG.get("rewriter") == "down" and d["model"] == TRANSLATOR:   # only the first translator is down
                return self.send(502, {"error": {"code": "server_is_overloaded"}})
            return self.send(200, {"choices": [{"message": {"content": fake_translate(d["messages"][0]["content"])}}]})
        system = next(m["content"] for m in d["messages"] if m["role"] == "system")
        STATE["calls"].append("PROBE")
        if CFG.get("mode") == "tos":
            return self.send(403, {"error": {"status": "PERMISSION_DENIED", "message": "TOS_VIOLATION"}})
        if any(t in apply_rules(system) for t in CFG["triggers"]):
            return self.send(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}})
        self.send(200, {"choices": []})


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])), Handler).serve_forever()
