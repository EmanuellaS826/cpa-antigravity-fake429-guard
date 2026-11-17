#!/usr/bin/env python3
"""Budget runs out mid-search: the work is parked and resumed until it finishes.
usage: resume_test.py NAME TRIGGERS_JSON SYSTEM_PROMPT_FILE HOURLY_BUDGET [PATCH_MODE]
Between resumes the test clears the budget and the waiting times instead of sleeping."""
import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import cpa_guard as cg  # noqa: E402

name, trig, sysf, hb = sys.argv[1:5]
mode = sys.argv[5] if len(sys.argv) > 5 else "replace"
D = os.path.join(tempfile.gettempdir(), "cpa-guard-tests", name)
shutil.rmtree(D, ignore_errors=True)
for s in ("cpa", "logs", "state"):
    os.makedirs(os.path.join(D, s))
open(f"{D}/cpa/config.yaml", "w").write('api-keys:\n  - "k1"\nrouting:\n  retry:\n    request-retry: 1\n')
json.dump({"triggers": json.loads(trig), "mode": "normal", "patch": mode}, open(f"{D}/fake.json", "w"))
port = 19000 + os.getpid() % 900
fake = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_cpa.py"), f"{D}/fake.json", str(port)])
time.sleep(0.7)
try:
    body = json.dumps({"model": "gemini-3.8-flash-high", "messages": [
        {"role": "system", "content": open(sysf).read()}, {"role": "user", "content": "hi"}]})
    logp = f"{D}/logs/error-v1-chat-completions-test.log"
    open(logp, "w").write("=== REQUEST INFO ===\nURL: /v1/chat/completions\n=== HEADERS ===\nUser-Agent: test\n"
                          "=== REQUEST BODY ===\n" + body + "\n=== RESPONSE ===\nStatus: 429\nRESOURCE_EXHAUSTED\n")
    os.environ.update(CPA_API_KEY="k1", CPA_MGMT_KEY="m1")
    ns = argparse.Namespace(
        cpa_dir=f"{D}/cpa", log_dir=f"{D}/logs", state_dir=f"{D}/state", base=f"http://127.0.0.1:{port}",
        model_regex=r"^(gemini-|claude-|gpt-oss)", hourly_budget=int(hb), daily_budget=1000, max_requests=30,
        candidates=3, pause=0, cooldown_wait=0, reload_wait=0.1, rewrite_model="gpt-6-luna", poll=1, notify_cmd="",
        no_apply=False, dry_run=False, once=None, status=False, resume=False)
    g = cg.Guard(ns)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        g.handle(logp)
        for _ in range(6):
            if not g.st.d["pending"]:
                break
            g.st.d["hits"], g.st.d["last_start"], g.st.d["quiet_until"] = [], 0, 0
            for it in g.st.d["pending"].values():
                it["retry_at"] = 0
            g.resume_pending()
    print("\n".join(l for l in out.getvalue().splitlines()
                    if "] " in l[:40] and l[20:21] == "[" or "行：" in l or "已加规则" in l))
    d = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/dump"))
    print("probes", d["calls"].count("PROBE"))
    for p in d["patterns"]:
        print("  rule:", p["pattern"][:60], "->", p["replacement"][:60])
    print("results", {k: v["status"] for k, v in g.st.d["handled"].items()}, "parked", list(g.st.d["pending"]))
finally:
    fake.kill()
