#!/usr/bin/env python3
"""Crash recovery and budget deferral.
1. The state says two rules were written but never verified; one of them was edited by a human since.
   Expected: the untouched one is removed, the edited one and the other rules stay.
2. The 429 budget is used up when a new fake 429 arrives.
   Expected: DEFER, and no investigation attempt is counted."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(os.path.dirname(HERE), "cpa_guard.py")
D = os.path.join(tempfile.gettempdir(), "cpa-guard-tests", "recover")
shutil.rmtree(D, ignore_errors=True)
for s in ("cpa", "logs", "state"):
    os.makedirs(os.path.join(D, s))
open(f"{D}/cpa/config.yaml", "w").write('api-keys:\n  - "k1"\nrouting:\n  retry:\n    request-retry: 1\n')
json.dump({"triggers": ["X"], "mode": "normal", "patch": "replace"}, open(f"{D}/fake.json", "w"))
port = 19950
fake = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_cpa.py"), f"{D}/fake.json", str(port)])
time.sleep(0.7)
base = f"http://127.0.0.1:{port}"


def call(method, path, body=None):
    req = urllib.request.Request(base + path, method=method, headers={"Content-Type": "application/json"},
                                 data=None if body is None else json.dumps(body).encode())
    return json.loads(urllib.request.urlopen(req).read() or b"null")


try:
    call("PATCH", "/v8/management/config/oauth/providers/antigravity", {"sensitive-patterns-replace": [
        {"pattern": "keep-me", "replacement": ""}, {"pattern": "orphan", "replacement": ""},
        {"pattern": "human-added", "replacement": "x"}, {"pattern": "edited", "replacement": "changed by a human"}]})
    json.dump({"unverified": [["orphan", ""], ["edited", ""]]}, open(f"{D}/state/state.json", "w"))
    open(f"{D}/logs/x.log", "w").write("nothing")
    env = dict(os.environ, CPA_API_KEY="k1", CPA_MGMT_KEY="m1")
    args = ["--cpa-dir", f"{D}/cpa", "--log-dir", f"{D}/logs", "--state-dir", f"{D}/state", "--base", base]
    print(subprocess.run([sys.executable, GUARD, *args, "--once", f"{D}/logs/x.log"], env=env,
                         capture_output=True, text=True).stdout)
    print("rules now:", [p["pattern"] for p in call("GET", "/dump")["patterns"]], "(expected: keep-me, human-added, edited)")

    s = json.load(open(f"{D}/state/state.json"))
    s["hits"] = [[time.time(), 10]]
    json.dump(s, open(f"{D}/state/state.json", "w"))
    body = json.dumps({"model": "gemini-x", "messages": [{"role": "system", "content": "hello X\nline2"},
                                                         {"role": "user", "content": "hi"}]})
    open(f"{D}/logs/error-a.log", "w").write("=== REQUEST INFO ===\nURL: /v1/chat/completions\n=== HEADERS ===\n"
                                             "User-Agent: test\n=== REQUEST BODY ===\n" + body +
                                             "\n=== RESPONSE ===\nStatus: 429\nRESOURCE_EXHAUSTED\n")
    print(subprocess.run([sys.executable, GUARD, *args, "--once", f"{D}/logs/error-a.log"], env=env,
                         capture_output=True, text=True).stdout)
    print("investigation attempts counted:", json.load(open(f"{D}/state/state.json"))["attempts"], "(expected: {})")
finally:
    fake.kill()
