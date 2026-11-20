#!/usr/bin/env python3
"""cpa_guard: watch CPA error logs, find the system-prompt line behind an Antigravity
fake 429, add a rule through CPA's management API and verify it. Run ON THE CPA SERVER.
Python 3 standard library only; needs find_trigger.py in the same folder.

Every probe that comes back 429 is a real hit on a Google account, and too many of
them can get the account banned. So the guard spends a budget (see --hourly-budget):
  * a probe is only sent while the budget has room; when it runs out the work is
    parked (narrowest trigger text so far + triggers already confirmed) and resumed later;
  * cheap evidence first: lines that are new compared with the last prompt of the
    same agent, then the first line / "powered by" / "You are" / URL / heading lines;
  * a 403 / TOS_VIOLATION (in a probe or in real traffic) halts every probe until a
    human runs --resume;
  * after a real-quota 429 (control probe fails) nothing is probed for 30 minutes.

Rules are only ever appended to oauth.providers.antigravity.sensitive-patterns-replace
(GET the whole provider object, PATCH the list, GET again and require that only the list
changed). A plain trigger line gets a whole-line delete regex. A line with a path, command,
tool name or placeholder is first reworded by a NON-Antigravity model (--rewrite-model),
keeping those tokens; whole-line delete is the last resort. Every rule is checked by sending
the trigger text through CPA (must be 200); a rule that fails is removed again. A rule that
was written but not yet checked is recorded in the state and removed after a crash.
If the management API ever behaves unexpectedly, automatic rule writing stops until --resume.

Usage:
  python3 cpa_guard.py                      # run forever (use the systemd unit)
  python3 cpa_guard.py --once LOG --dry-run # analyse one log, send nothing
  python3 cpa_guard.py --status             # budget, halted flag, pending work
  python3 cpa_guard.py --resume             # clear the halted / apply-disabled flags
Keys: CPA_API_KEY (client key; read from config.yaml when unset), CPA_MGMT_KEY.
"""

import argparse
import urllib.error
import urllib.request
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import find_trigger as ft  # noqa: E402

DAY = 86400
DEDUP_SECONDS = 6 * 3600       # same system prompt is not investigated again for this long
SHORT_DEDUP = 1800             # after "deferred" / "partial"
GAP_SECONDS = 300              # minimum time between two investigations
QUIET_SECONDS = 1800           # pause after a real-quota 429
RETRY_PENDING = 900            # how long to wait before resuming parked work
RECOVER_EVERY = 60             # retry removing an unverified rule this often
MAX_ATTEMPTS = 3               # resumes of one parked investigation
MAX_PENDING = 5
MAX_RULES_PER_DAY = 3
MAX_INVESTIGATIONS_PER_AGENT_DAY = 3
MAX_LINE_CHARS = 300
MAX_SENTENCES = 12        # a trigger line with 2..12 sentences is narrowed to the sentence(s) that 429 alone
KEEP_BACKUPS = 10
UNSAFE_LINE = re.compile(r"[`$\\/<>{}_]|^\s*[-*]\s*[\w.-]+\s*:")   # paths, commands, tool/skill names
PROVIDER = "/config/oauth/providers/antigravity"
PATS = "sensitive-patterns-replace"
WORDS = "sensitive-words-replace"


class BudgetExhausted(Exception):
    pass


class Halted(Exception):
    pass


class DeadRule(Exception):
    pass


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


# ------------------------------------------------------------------- state

class State:
    def __init__(self, path):
        self.path = path
        self.d = {"halted": None, "apply_disabled": None, "hits": [], "handled": {}, "pending": {},
                  "known": {}, "rules": [], "attempts": {}, "cap_notified": {}, "unverified": [],
                  "last_start": 0, "quiet_until": 0, "resumed_at": 0}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                self.d.update(json.load(fh))

    def refresh_flags(self):
        """--resume runs in another process and edits the file; pick that up."""
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
            self.d["halted"], self.d["apply_disabled"] = d.get("halted"), d.get("apply_disabled")
        except (OSError, ValueError):
            pass

    def save(self):
        now = time.time()
        d = self.d
        d["handled"] = {k: v for k, v in d["handled"].items() if v["time"] > now - 3 * DAY}
        d["hits"] = [h for h in d["hits"] if h[0] > now - DAY]
        d["rules"] = [t for t in d["rules"] if t > now - DAY]
        d["attempts"] = {k: [t for t in v if t > now - DAY] for k, v in d["attempts"].items()}
        d["cap_notified"] = {k: t for k, t in d["cap_notified"].items() if t > now - DAY}
        if len(d["pending"]) > MAX_PENDING:
            keep = sorted(d["pending"].items(), key=lambda kv: kv[1].get("retry_at", 0))[-MAX_PENDING:]
            d["pending"] = dict(keep)
        tmp = self.path + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)


class Budget:
    """Upstream 429 hits caused by the guard. One 429 probe = 1 + request-retry hits,
    because CPA retries a failed request upstream (seen in main.log: 2 attempts per 429)."""

    def __init__(self, state, hourly, daily, cost):
        self.st, self.hourly, self.daily, self.cost = state, hourly, daily, cost

    def used(self, window):
        now = time.time()
        return sum(n for ts, n in self.st.d["hits"] if ts > now - window)

    def left(self):
        return min(self.hourly - self.used(3600), self.daily - self.used(DAY))

    def charge(self):
        self.st.d["hits"].append([time.time(), self.cost])
        self.st.save()


# ----------------------------------------------------------------- helpers

def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def read_text_lossy(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def request_retry(cfg_text):
    m = re.search(r"^\s*request-retry:\s*(\d+)", cfg_text, re.M)
    return int(m.group(1)) if m else 1


def client_key(cfg_text):
    m = re.search(r"api-keys:\s*\n(?:\s*#.*\n)*\s*-\s*[\"']?([^\"'\s]+)", cfg_text)
    return m.group(1) if m else ""


def agent_key(system):
    for line in system.splitlines():
        if line.strip():
            return hashlib.sha256(line.strip()[:40].lower().encode()).hexdigest()[:12]
    return ""


def re2_escape(s):
    return "".join("\\" + ch if ch in "\\.+*?()|[]{}^$" else ch for ch in s)


def line_pattern(line):
    """Go-RE2 whole-line delete regex; digit runs and blanks are generalised (ASCII only,
    because RE2's \\d and \\s are ASCII while Python's are not)."""
    parts = re.split(r"([0-9]+|[ \t]+)", line.strip())
    body = "".join("[0-9]+" if p[0] in "0123456789" else ("[ \\t]+" if p[0] in " \t" else re2_escape(p))
                   for p in parts if p)
    return "(?m)^[ \\t]*" + body + "[ \\t]*\\r?$\\n?"


def sentence_pattern(sentence):
    """Go-RE2 regex for one sentence inside a line (not anchored), blanks and digit runs generalised."""
    parts = re.split(r"([0-9]+|[ \t]+)", sentence.strip())
    return "".join("[0-9]+" if p[0] in "0123456789" else ("[ \\t]+" if p[0] in " \t" else re2_escape(p))
                   for p in parts if p)


def split_sentences(line):
    """Sentences of one line; pieces shorter than 20 characters (e.g. after "e.g.") stay with the previous one."""
    out = []
    for piece in re.split(r"(?<=[.!?。！？])\s+", line.strip()):
        if out and (len(piece) < 20 or len(out[-1]) < 20):
            out[-1] += " " + piece
        elif piece:
            out.append(piece)
    return out


def transform(patterns, words, text):
    """Apply CPA's existing rules locally (words first, then patterns; the real order is unverified,
    the later check through CPA settles it)."""
    for w, r in words.items():
        if w:
            text = text.replace(w, str(r))
    for p in patterns:
        try:
            repl = str(p.get("replacement") or "")
            text = re.sub(str(p.get("pattern", "")), lambda m, r=repl: r, text)
        except re.error:
            pass
    return text


def tool_names(req):
    """Names of the tools the agent sent with the request (OpenAI, Responses, Claude, Gemini formats)."""
    names = set()
    for t in req.get("tools") or []:
        if not isinstance(t, dict):
            continue
        f = t.get("function") if isinstance(t.get("function"), dict) else t
        if isinstance(f.get("name"), str) and f["name"]:
            names.add(f["name"])
        for d in t.get("functionDeclarations") or t.get("function_declarations") or []:
            if isinstance(d, dict) and isinstance(d.get("name"), str) and d["name"]:
                names.add(d["name"])
    return names


def tools_in(line, tools):
    return sorted(t for t in tools if re.search(r"(?<![\w-])" + re.escape(t) + r"(?![\w-])", line))


def make_rules(found, system, patterns, words, tools=()):
    """Returns (specs, manual). spec = {pattern, line, text, tools}. The rule replaces the line with its
    Chinese translation (tool names, paths, commands kept); a line is never deleted."""
    eff_system = transform(patterns, words, system)
    specs, manual = [], []
    for _, text in found:
        cands = [(l.strip(), transform(patterns, words, l).strip()) for l in text.splitlines() if l.strip()]
        cands = [(o, e) for o, e in cands if e]     # a line an existing rule already deletes is not the culprit
        if not cands:
            manual.append(("现有规则已经把触发行删光了却仍然 429", text.strip()))
            continue
        plain = [(o, e) for o, e in cands
                 if len(e) <= MAX_LINE_CHARS and not UNSAFE_LINE.search(e) and not tools_in(e, tools)]
        _, line = min(plain or cands, key=lambda oe: len(oe[1]))
        pat = line_pattern(line)   # built from the line AFTER the existing rules, because new rules run later
        hits = len(re.findall(re.compile(pat, re.ASCII), eff_system))
        if not 1 <= hits <= 3:
            manual.append((f"规则在原提示里匹配 {hits} 处（要求 1 到 3 处）", text.strip()))
            continue
        specs.append({"pattern": pat, "line": line, "text": text, "tools": tools_in(line, tools)})
    return specs, manual


def line_numbers(system, text):
    """1-based numbers of the lines of `text` in the original prompt (a resumed search works on a
    shortened copy, so its own numbers are not the original ones)."""
    want = {l.strip() for l in text.splitlines() if l.strip()}
    return [i + 1 for i, l in enumerate(system.splitlines()) if l.strip() in want]


def norm(cfg):
    """Provider object in a comparable form: rule items reduced to (pattern, replacement)."""
    d = {k: v for k, v in cfg.items() if k != PATS}
    d[PATS] = [(str(p.get("pattern", "")), str(p.get("replacement") or "")) for p in cfg.get(PATS) or []]
    return d


def same(a, b):
    return norm(a) == norm(b)


def classify(path):
    """('tos', None) | ('fake429', (system, model, url, tool names)) | (None, None). Looks only at the response part."""
    try:
        text = read_text_lossy(path)
    except OSError:
        return None, None
    head, sep, rest = text.partition("=== REQUEST BODY ===")
    if not sep:
        return None, None
    m = re.search(r"\n=== ", rest)
    if not m:
        return None, None
    body, tail = rest[:m.start()], rest[m.end():]
    if ft.PROBE_UA in head or ft.USER_MESSAGE in body:
        return None, None            # our own probe
    if "Status: 403" in tail and "TOS_VIOLATION" in tail:
        return "tos", None
    if "Status: 429" not in tail or "RESOURCE_EXHAUSTED" not in tail:
        return None, None
    if "cooling down" in tail or "model_cooldown" in tail:
        return None, None            # CPA's own local cooldown, never reached Google
    try:
        url, req = ft.read_log(path)
    except ft.Abort:
        return None, None
    return "fake429", (ft.extract_system(req), ft.model_of(url, req, ""), url, sorted(tool_names(req)))


def notify(kind, title, detail):
    """Bark push (env BARK_URL, e.g. https://api.day.app/<key>). Failures are only logged."""
    bark = os.environ.get("BARK_URL", "").rstrip("/")
    if not bark:
        return
    req = urllib.request.Request(bark, method="POST", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"title": "CPA 守护：" + title, "body": detail[:3000] or kind,
                                                  "group": "cpa-guard"}, ensure_ascii=False).encode("utf-8"))
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if json.loads(resp.read().decode("utf-8") or "{}").get("code", 200) != 200:
                log("Bark 通知失败：服务器没有接受")
    except (OSError, ValueError) as exc:
        log(f"Bark 通知失败：{exc.__class__.__name__}")


NOTIFY_KINDS = {"FIXED", "FOUND", "PARTIAL", "MANUAL", "HALT", "GENUINE", "INCONCLUSIVE", "ROLLED_BACK",
                "ROLLBACK", "PAUSE", "TEST"}
STATUS_TITLE = {"fixed": "已自动修好", "found": "找到触发句（只报告，没改规则）", "partial": "修好了一部分",
                "manual": "找到触发句，但没法自动处理", "genuine": "不是提示词问题（账号可能真没额度）",
                "inconclusive": "这次没查出结果", "rolled_back": "规则没加成，已撤回", "deferred": "推迟"}


# ------------------------------------------------------------------- guard

class Guard:
    def __init__(self, a):
        self.a = a
        os.makedirs(a.state_dir, mode=0o700, exist_ok=True)
        os.makedirs(os.path.join(a.state_dir, "backups"), mode=0o700, exist_ok=True)
        self.st = State(os.path.join(a.state_dir, "state.json"))
        self.cfg_path = os.path.join(a.cpa_dir, "config.yaml")
        self.cost = 1 + request_retry(read_text(self.cfg_path))
        self.budget = Budget(self.st, a.hourly_budget, a.daily_budget, self.cost)
        self.api_key = os.environ.get("CPA_API_KEY") or client_key(read_text(self.cfg_path))
        self.mgmt_key = os.environ.get("CPA_MGMT_KEY", "")
        self.model_re = re.compile(a.model_regex)
        self.can_apply = not a.no_apply and bool(self.mgmt_key)
        if not a.no_apply and not self.mgmt_key:
            log("没有 CPA_MGMT_KEY：只报告，不改规则")
        self.seen = None          # log names handed to handle() (daemon mode only)
        self.tos_seen = None      # log names already checked for a 403 during probing
        self.next_recover = 0

    # ---- bookkeeping
    def event(self, kind, title, detail=""):
        rec = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "title": title, "detail": detail}
        with open(os.path.join(self.a.state_dir, "events.log"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log(f"[{kind}] {title}")
        for l in detail.splitlines():
            log("    " + l)
        if kind in NOTIFY_KINDS and not self.a.dry_run:
            notify(kind, title, detail)
        if self.a.notify_cmd:
            env = dict(os.environ, GUARD_KIND=kind, GUARD_TITLE=title, GUARD_DETAIL=detail)
            try:
                subprocess.run(self.a.notify_cmd, shell=True, env=env, timeout=30, check=False)
            except (OSError, subprocess.SubprocessError) as exc:
                log(f"通知命令失败：{exc}")

    def halt(self, reason):
        self.st.d["halted"] = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": reason}
        self.st.save()
        self.event("HALT", "已停止所有测试请求（人工 --resume 才会恢复）", reason)

    def disable_apply(self, reason, detail=""):
        self.st.d["apply_disabled"] = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": reason}
        self.st.save()
        self.event("MANUAL", "管理接口的行为和预期不同，已停止自动改规则（人工 --resume 才会恢复）",
                   reason + ("\n" + detail if detail else ""))

    def stopped(self):
        return bool(self.st.d["halted"]) or os.path.exists(os.path.join(self.a.state_dir, "STOP"))

    def logs(self, settled=False):
        """error-*.log names; settled=True leaves out files written in the last 2 seconds
        (CPA may still be writing them; they are picked up on a later pass)."""
        names = {f for f in os.listdir(self.a.log_dir) if f.startswith("error-") and f.endswith(".log")}
        if settled:
            limit = time.time() - 2
            names = {f for f in names if os.path.getmtime(os.path.join(self.a.log_dir, f)) < limit}
        return names

    def scan_new_logs(self):
        """While probing: check logs that appeared meanwhile for a real-traffic 403. This does not
        consume them; the main loop still hands them to handle() afterwards."""
        if self.tos_seen is None:
            return
        try:
            new = sorted(self.logs(settled=True) - self.tos_seen)
        except OSError:
            return
        for name in new:
            self.tos_seen.add(name)
            self.scan_tos(os.path.join(self.a.log_dir, name))

    def quarantined(self):
        """A rule was written but neither verified nor removed: no new probes or writes until
        recover() has removed it (the configuration is not what the guard thinks it is)."""
        return bool(self.st.d["unverified"])

    def new_tester(self, model):
        t = ft.Tester(self.a.base, self.api_key, self.mgmt_key, model, self.a.max_requests,
                      self.a.pause, self.a.cooldown_wait)

        def gate():
            self.scan_new_logs()       # a real-traffic 403 stops the investigation at once
            if self.stopped():
                raise Halted()
            if self.budget.left() < self.cost:
                raise BudgetExhausted()
        t.gate = gate
        t.on_429 = self.budget.charge
        return t

    def mark(self, fp, status, until_seconds=DEDUP_SECONDS):
        self.st.d["handled"][fp] = {"time": time.time(), "status": status, "until": time.time() + until_seconds}
        self.st.save()

    def refresh_cost(self):
        self.cost = 1 + request_retry(read_text(self.cfg_path))
        self.budget.cost = self.cost

    # ---- log intake
    def scan_tos(self, path):
        kind, _ = classify(path)
        if kind == "tos" and not self.st.d["halted"]:
            self.halt(f"真实流量里出现 403 TOS_VIOLATION（{os.path.basename(path)}）。账号可能被谷歌限制，别再发测试请求。")

    def handle(self, path):
        kind, info = classify(path)
        if kind == "tos":
            if self.a.dry_run:
                self.event("DRY", f"[试运行] {os.path.basename(path)} 是 403 TOS_VIOLATION（试运行不设停止标志）")
            else:
                self.scan_tos(path)
            return
        if kind != "fake429":
            return
        system, model, _, tools = info
        if not system.strip() or not self.model_re.search(model):
            return
        fp = hashlib.sha256(system.encode()).hexdigest()[:16]
        if self.a.dry_run:
            lines = system.splitlines(keepends=True)
            sus = ft.pick_suspects(lines, self.st.d["known"].get(agent_key(system), []))
            self.event("DRY", f"[试运行] 模型 {model}，指纹 {fp}，{len(lines)} 行，嫌疑行 {[i + 1 for i in sus]}，不发请求",
                       "\n".join(lines[i].strip()[:100] for i in sus))
            return
        now = time.time()
        done = self.st.d["handled"].get(fp)
        if fp in self.st.d["pending"] or (done and now < done.get("until", done["time"] + DEDUP_SECONDS)):
            return
        if self.stopped():
            log(f"已停止状态，跳过 {os.path.basename(path)}")
            return
        if self.quarantined():
            log(f"有没验证的规则还没撤回，先不调查 {os.path.basename(path)}")
            return
        if now < self.st.d["quiet_until"]:
            log("真实额度 429 后的静默期，跳过")
            return
        if now - self.st.d["last_start"] < GAP_SECONDS:
            log("距离上一次调查太近，跳过（真实的 429 之后还会再出现）")
            return
        akey = agent_key(system)
        if len([t for t in self.st.d["attempts"].get(akey, []) if t > now - DAY]) >= MAX_INVESTIGATIONS_PER_AGENT_DAY:
            if akey not in self.st.d["cap_notified"]:
                self.st.d["cap_notified"][akey] = now
                self.st.save()
                self.event("PAUSE", f"同一个 agent 24 小时内已经调查 {MAX_INVESTIGATIONS_PER_AGENT_DAY} 次，"
                                    "暂停自动调查它，24 小时后自动恢复（为了少发 429）",
                           f"模型 {model}，系统提示第一行：{system.strip().splitlines()[0][:120]}")
            return
        self.refresh_cost()
        if self.budget.left() < 2 * self.cost:      # checked before an attempt is counted
            self.mark(fp, "deferred", SHORT_DEDUP)
            self.event("DEFER", f"429 预算不够（剩 {self.budget.left()}，至少要 {2 * self.cost}），"
                                f"{SHORT_DEDUP // 60} 分钟内不再处理这个提示 {fp}")
            return
        log(f"新的上游 429：模型 {model}，系统提示 {len(system)} 字符，指纹 {fp}")
        self.st.d["last_start"] = now
        self.st.d["attempts"].setdefault(akey, []).append(now)
        self.st.save()
        self.investigate({"fp": fp, "system": system, "model": model, "narrowed": "", "attempts": 0, "tools": tools})

    # ---- investigation
    def investigate(self, item):
        fp, system, model = item["fp"], item["system"], item["model"]
        self.refresh_cost()
        need = (1 if item.get("found") else 2) * self.cost
        if self.budget.left() < need:
            log(f"429 预算不够（剩 {self.budget.left()}，至少要 {need}），{fp} 稍后再查")
            if fp not in self.st.d["pending"]:
                self.mark(fp, "deferred", SHORT_DEDUP)
            return
        tester = self.new_tester(model)
        prev = [tuple(f) for f in item.get("found_partial", [])]
        try:
            if item.get("found"):
                found, more = [tuple(f) for f in item["found"]], item.get("more", False)
            else:
                if tester.triggers(ft.NEUTRAL_SYSTEM, "对照组", reset_on_429=False):
                    self.st.d["quiet_until"] = time.time() + QUIET_SECONDS
                    tester.last_trigger = ""
                    if prev or item.get("narrowed"):     # resumed work: keep its progress
                        self.park(item, "对照组也是 429（账号可能真没额度），静默 30 分钟", tester)
                    else:
                        self.finish(fp, "genuine", "对照组也是 429：账号真的没额度或被限流，不是提示词问题。没有加规则，静默 30 分钟。")
                    return
                text = item.get("narrowed") or system
                more = True
                if prev and item.get("rest_unchecked"):
                    more = tester.triggers(text, "去掉已找到的触发句后的完整提示", fresh=True)
                new = []
                if more:
                    new, more = ft.search(tester, text.splitlines(keepends=True),
                                          self.st.d["known"].get(agent_key(system), []),
                                          self.a.candidates, max(1, 3 - len(prev)), False, assume_full=True)
                found = prev + new
        except Halted:
            self.event("HALT", "收到停止信号，调查中止", fp)
            return
        except BudgetExhausted:
            self.park(item, "429 预算用完", tester)
            return
        except ft.LimitReached:
            self.park(item, "达到单次调查请求上限", tester)
            return
        except ft.TosViolation as exc:
            self.halt(str(exc))
            return
        except ft.Abort as exc:
            self.park(item, "结果不确定：" + str(exc)[:80], tester)
            return
        self.st.d["pending"].pop(fp, None)
        self.apply_result(item, tester, found, more)

    def park(self, item, why, tester=None, found=None, more=False):
        """Save interrupted work. Either `found` (ready to write) or the search progress in `tester`."""
        fp = item["fp"]
        if found is not None:
            item = dict(item, found=[list(f) for f in found], more=more)
        elif tester is not None:
            partial = [list(f) for f in item.get("found_partial", [])] + [list(f) for f in tester.found_so_far]
            if tester.found_so_far:
                narrowed, unchecked = tester.last_trigger, tester.rest_unchecked
            elif tester.last_trigger:
                narrowed, unchecked = tester.last_trigger, False
            else:      # stopped before any 429: keep what was saved before
                narrowed, unchecked = item.get("narrowed", ""), item.get("rest_unchecked", False)
            item = dict(item, found_partial=partial, narrowed=narrowed, rest_unchecked=unchecked)
        known = item.get("found") or item.get("found_partial") or []
        if not item.get("found") and known and (not item.get("narrowed") or item.get("attempts", 0) + 1 >= MAX_ATTEMPTS):
            # stop searching; what is confirmed still gets its rule (later, when the budget allows)
            more = bool(item.get("narrowed"))
            item = dict(item, found=known, more=more, attempts=0)
            why += "；不再接着找，先给已确认的触发句加规则" if more else ""
        if not item.get("found") and not item.get("narrowed"):
            self.finish(fp, "inconclusive", f"{why}，而且还没有找到可以缩小的触发文字，不暂存。")
            return
        if item.get("attempts", 0) + 1 >= MAX_ATTEMPTS:
            detail = f"{why}，已尝试 {MAX_ATTEMPTS} 次仍没完成，先放下，下次这个 agent 再出 429 会重新查。"
            if known:
                detail += "\n已经确认的触发句：\n" + "\n".join("  " + t.strip()[:200] for _, t in known)
            self.finish(fp, "inconclusive", detail)
            return
        item = dict(item, attempts=item.get("attempts", 0) + 1, retry_at=time.time() + RETRY_PENDING)
        self.st.d["pending"][fp] = item
        self.st.save()
        size = len(item.get("narrowed") or "")
        self.event("DEFER", f"{why}，已暂存（已确认 {len(known)} 处，剩余范围 {size} 字符），"
                            f"约 {RETRY_PENDING // 60} 分钟后接着查 {fp}")

    def finish(self, fp, status, detail):
        self.st.d["pending"].pop(fp, None)
        self.mark(fp, status, SHORT_DEDUP if status in ("partial", "deferred") else DEDUP_SECONDS)
        self.event(status.upper(), f"{STATUS_TITLE.get(status, status)}（{fp[:8]}）", detail)

    # ---- rules
    def reword(self, line, rejected, tools=(), soften=False):
        """The line translated into Simplified Chinese by a NON-Antigravity model (so the Google account is
        not touched and the quoted trigger cannot 429 it), with paths, commands, tool names, URLs and code
        tokens kept exactly; or None. soften=True (used after a plain translation still 429ed): a Chinese
        paraphrase with the same intent that names no specific sensitive technique, product or topic."""
        model = self.a.rewrite_model
        if not model or self.model_re.search(model):
            return None
        if soften:
            prompt = ("The following line, taken from an AI assistant's system prompt, is rejected by a content filter "
                      "even when translated, because of what it mentions. Rewrite it in Simplified Chinese as one short, "
                      "general instruction with the same intent, WITHOUT naming any specific attack technique, weapon, "
                      "malware, exploit, tool category, product, company, political or sexual topic, or other sensitive "
                      "term. Keep every file path, command, tool name, placeholder (like <tool>), URL and code token "
                      "EXACTLY as written. Reply with the rewritten line only, on one line, no quotes, no explanation."
                      "\n\nLine:\n" + line)
        else:
            prompt = ("Translate the following single line, taken from an AI assistant's system prompt, into Simplified "
                      "Chinese. Keep its meaning and instructions exactly. Keep every file path, command, tool name, "
                      "placeholder (like <tool>), URL, code token and Markdown marker (#, -, *, `) EXACTLY as written, "
                      "untranslated. Reply with the translated line only, on one line, no quotes, no explanation."
                      "\n\nLine:\n" + line)
        if tools:
            prompt += "\n\nThese are tool names and must appear unchanged: " + ", ".join(tools)
        if rejected:
            prompt += "\n\nThese versions were rejected, use different wording:\n" + "\n".join("- " + r for r in rejected)
        req = urllib.request.Request(
            self.a.base + "/v1/chat/completions",
            data=json.dumps({"model": model, "temperature": 1.0, "max_tokens": 300,
                             "messages": [{"role": "user", "content": prompt}]}).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json",
                     "User-Agent": ft.PROBE_UA}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                cand = json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"].strip()
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            log(f"翻译模型没有给出结果（{exc.__class__.__name__}）")
            return None
        protected = [t for t in line.split() if UNSAFE_LINE.search(t) or re.search(r"[a-z][A-Z]", t)] + list(tools)
        if (not cand or "\n" in cand or cand == line or cand in rejected or len(cand) > 2 * len(line) + 50
                or not re.search(r"[\u4e00-\u9fff]", cand) or any(t not in cand for t in protected)):
            rejected.append(cand or "(空)")
            return self.reword(line, rejected, tools, soften) if len(rejected) < 3 else None
        return cand

    def install(self, tester, spec):
        """Replace the trigger line with its Chinese translation and check through CPA that the trigger
        text no longer 429s. When a translation still 429s, the remaining tries ask for a softened Chinese
        paraphrase (same intent, no specific sensitive terms). Up to 3 versions are tried; the line is never
        deleted. A rule that fails or whose check is interrupted is removed again."""
        rejected, soften = [], False
        for _ in range(3):
            cand = self.reword(spec["line"], rejected, spec.get("tools", ()), soften)
            if cand is None:
                break
            rejected.append(cand)
            rule = {"pattern": spec["pattern"], "replacement": cand.replace("$", "$$")}
            self.add_rule(rule)
            try:
                time.sleep(self.a.reload_wait)
                bad = tester.triggers(ft.NEUTRAL_SYSTEM + "\n" + spec["text"], "验证规则：触发文字", fresh=True)
            except BaseException:
                self.remove_rules([rule], "验证没有完成")
                raise
            if not bad:     # verified: leaves the journal and counts toward the daily limit in one save
                self.st.d["unverified"] = [u for u in self.st.d["unverified"] if u != [rule["pattern"], rule["replacement"]]]
                self.st.d["rules"].append(time.time())
                self.st.save()
                return f"{'改写成不含敏感词的中文' if soften else '改成中文'}：{cand}"
            if not self.remove_rules([rule], "中文版仍然 429"):
                raise ft.Abort("没用的规则撤回失败")
            soften = True   # the meaning itself is filtered: a plain translation will not do
        raise DeadRule()

    def narrow(self, tester, spec, eff_system):
        """A long trigger line often holds one bad sentence among harmless ones, and translating the
        whole line keeps that sentence's meaning (still 429). So each sentence is first sent alone
        (a 200 costs nothing, a 429 one hit) and only the sentences that 429 by themselves become rules.
        When none does alone (the combination triggers), or a sentence rule would not match cleanly,
        the whole line is kept as the unit."""
        sents = split_sentences(spec["line"])
        if not 2 <= len(sents) <= MAX_SENTENCES:
            return [spec]
        log(f"触发行有 {len(sents)} 句，先逐句找出触发的那句……")
        bad = [s for i, s in enumerate(sents, 1)
               if tester.triggers(ft.NEUTRAL_SYSTEM + "\n" + s, f"触发行第 {i} 句", fresh=True)]
        subs = [{"pattern": sentence_pattern(s), "line": s, "text": s, "tools": tools_in(s, spec.get("tools", ()))}
                for s in bad]
        if not subs or len(subs) == len(sents) or any(
                not 1 <= len(re.findall(re.compile(s["pattern"], re.ASCII), eff_system)) <= 3 for s in subs):
            log("单句定位不了（没有单独触发的句子，或句子规则匹配不干净），整行翻译")
            return [spec]
        return subs

    def apply_result(self, item, tester, found, more):
        fp, system = item["fp"], item["system"]
        report = [f"找到 {len(found)} 处触发句（已复测）："]
        report += [f"  第 {','.join(map(str, line_numbers(system, t) or nums))} 行：{t.strip()[:200]}" for nums, t in found]
        writable = self.can_apply and not self.st.d["apply_disabled"]
        try:
            before = self.get_config()
        except ft.Abort as exc:
            if writable:     # never write from a list that was not just read
                self.park(item, f"读不到 CPA 现有规则（{exc}），先不写", found=found, more=more)
                return
            before = {PATS: [], WORDS: {}}
        pats_old, words = before.get(PATS) or [], before.get(WORDS) or {}
        specs, manual = make_rules(found, system, pats_old, words, item.get("tools", ()))
        report += [f"没法自动生成规则：{reason}：{text[:200]}" for reason, text in manual]
        if not specs:
            self.finish(fp, "manual", "\n".join(report))
            return
        if not writable:
            why = "--no-apply 或没有管理密钥" if not self.can_apply else "自动改规则已停止（见之前的 MANUAL 事件）"
            report.append(f"只报告，没改规则（{why}）。如果打开自动处理，会这样做：")
            report += [f"  - {s['line'][:200]}\n    → 请 {self.a.rewrite_model or '（没配翻译模型）'} 翻译成中文"
                       + (f"，工具名 {', '.join(s['tools'])} 原样保留" if s.get("tools") else "")
                       + "，路径、命令原样保留；翻译后仍 429 就不处理（不删行）"
                       for s in specs]
            self.finish(fp, "found", "\n".join(report))
            return
        if len(self.st.d["rules"]) + len(specs) > MAX_RULES_PER_DAY:
            report.append(f"24 小时内已加 {len(self.st.d['rules'])} 条规则，上限 {MAX_RULES_PER_DAY}，这次不自动加。")
            self.finish(fp, "manual", "\n".join(report))
            return
        have = {str(p.get("pattern")) for p in pats_old}
        specs = [s for s in specs if s["pattern"] not in have]
        if not specs:
            self.finish(fp, "manual", "\n".join(report + ["这些规则已经在 CPA 里，但仍然 429。"]))
            return
        if self.budget.left() < self.cost:     # a failed verification costs one 429
            self.park(item, "预算不够做规则验证", found=found, more=more)
            return
        if self.quarantined():
            self.park(item, "有没验证的规则还没撤回，先不写", found=found, more=more)
            return
        self.backup(before)

        tester.sent = 0
        eff = transform(pats_old, words, system)
        done, dead, finished, full_ok, stop = [], [], set(), None, None
        try:
            for spec in specs:
                for sub in self.narrow(tester, spec, eff):
                    try:
                        done.append((sub, self.install(tester, sub)))
                    except DeadRule:
                        dead.append(sub)
                finished.add(spec["text"])
            if done and not (more or manual or dead):
                full_ok = not tester.triggers(system, "验证：原始完整提示", fresh=True)
        except (ft.Abort, ft.LimitReached, BudgetExhausted, Halted) as exc:
            stop = exc
        report += [f"已加规则（{how}）：{spec['line'][:80]}" for spec, how in done]
        report += [f"这条没能处理：{s['line'][:200]}（翻译成中文后仍然 429，或翻译不合格；按要求不删行，没加规则）"
                   for s in dead]
        if isinstance(stop, ft.TosViolation):
            self.halt(str(stop))
        if stop is not None:
            left = ("有没验证的规则还没撤回成功，会自动重试撤回，撤回前不做别的。"
                    if self.st.d["unverified"] and not self.st.d["apply_disabled"]
                    else "没验证的规则可能还在 CPA 里，见上面的 MANUAL 事件。" if self.st.d["unverified"]
                    else "没验证完的规则已撤回。")
            report.append(f"没有做完（{stop.__class__.__name__}: {str(stop)[:100]}）：已验证有效的规则保留；{left}")
            rest = [f for f in found if f[1] not in finished and any(s["text"] == f[1] for s in specs)]
            self.finish(fp, "partial" if done else "rolled_back", "\n".join(report))
            if rest and not self.st.d["apply_disabled"]:   # confirmed, rule not written yet: no need to search again
                self.park(item, "规则还没写完", found=rest, more=more)
        elif not done:
            self.finish(fp, "manual", "\n".join(report))
        elif full_ok:
            self.st.d["known"][agent_key(system)] = [l.rstrip("\n") for l in system.splitlines()]
            report.append("验证通过：触发文字和原始完整提示经过 CPA 都返回 200。")
            self.finish(fp, "fixed", "\n".join(report))
        else:
            report.append("规则有效，但原始完整提示可能还有别的触发句；规则保留，这个 agent 30 分钟后再出 429 时接着查。")
            self.finish(fp, "partial", "\n".join(report))

    def backup(self, before):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bdir = os.path.join(self.a.state_dir, "backups")
        files = ((f"config.yaml.{stamp}", read_text(self.cfg_path)),
                 (f"antigravity.{stamp}.json", json.dumps(before, ensure_ascii=False, indent=1)))
        for name, text in files:
            with open(os.open(os.path.join(bdir, name), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                      "w", encoding="utf-8") as fh:
                fh.write(text)
        names = sorted(os.listdir(bdir))
        for prefix in ("config.yaml.", "antigravity."):
            for old in [n for n in names if n.startswith(prefix)][:-KEEP_BACKUPS]:
                os.remove(os.path.join(bdir, old))

    # ---- management API
    def api(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.a.base + ft.MGMT + path, data=data, method=method,
                                     headers={"Authorization": "Bearer " + self.mgmt_key,
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as exc:
            raise ft.Abort(f"管理接口 {method} {path} 返回 HTTP {exc.code}")
        except (OSError, ValueError) as exc:
            raise ft.Abort(f"管理接口出错：{exc.__class__.__name__}: {exc}")

    def get_config(self):
        """The whole oauth.providers.antigravity object, validated."""
        d = self.api("GET", PROVIDER)
        if not isinstance(d, dict):
            raise ft.Abort("管理接口返回的不是对象")
        pats, words = d.get(PATS) or [], d.get(WORDS) or {}
        if (not isinstance(pats, list) or not isinstance(words, dict)
                or not all(isinstance(p, dict) and isinstance(p.get("pattern"), str) for p in pats)):
            raise ft.Abort("管理接口返回的规则格式不认识")
        return d

    def drop_unverified(self, rules):
        gone = [[r["pattern"], r["replacement"]] for r in rules]
        self.st.d["unverified"] = [u for u in self.st.d["unverified"] if u not in gone]
        self.st.save()

    def add_rule(self, rule):
        """GET, append, PATCH, GET again. Only the pattern list may change, and only by this rule."""
        if self.quarantined():
            raise ft.Abort("有没验证的规则还没撤回，先不写新的")
        before = self.get_config()          # failure here: nothing was written
        pats = before.get(PATS) or []
        if any(p["pattern"] == rule["pattern"] for p in pats):
            raise ft.Abort("这条规则已经在列表里（上一次撤回可能没成功）")
        expected = dict(before, **{PATS: pats + [rule]})
        self.st.d["unverified"].append([rule["pattern"], rule["replacement"]])   # removed after a crash if never verified
        self.st.save()
        err = None
        try:
            self.api("PATCH", PROVIDER, {PATS: pats + [rule]})
        except ft.Abort as exc:
            err = exc
        try:
            after = self.get_config()
        except ft.Abort as exc:
            raise ft.Abort(f"写入后读不回规则（{exc}）；规则可能已经生效，连上以后会自动撤回，撤回前不做别的")
        if same(after, expected):
            return
        if same(after, before):
            self.drop_unverified([rule])
            if err:
                raise err
            self.disable_apply("PATCH 返回成功，但规则列表没有变化")
            raise ft.Abort("PATCH 没有生效")
        detail = (f"写入前 {len(pats)} 条规则，预期 {len(pats) + 1} 条，读回 {len(after.get(PATS) or [])} 条；"
                  f"备份在 state/backups/antigravity.*.json")
        # No automatic repair: whether the difference comes from the PATCH or from a human editing the panel
        # at the same moment cannot be told apart, and another write could make it worse.
        changed = [k for k in set(before) | set(after) if k != PATS and norm(before).get(k) != norm(after).get(k)]
        if changed:
            detail += f"\n规则列表以外的配置项也变了：{', '.join(sorted(changed))}（例如词替换没了，要尽快按备份恢复）。"
        detail += "\n没有再写（再写可能更乱）。"
        self.disable_apply("写入后读回的配置和预期不一致", detail)
        raise ft.Abort("写入后读回的配置和预期不一致")

    def remove_rules(self, rules, why):
        """Remove these exact rules (pattern AND replacement, last occurrence) from the CURRENT list, so
        rules added or edited in the panel meanwhile stay. A rule whose replacement was changed by a
        human is no longer ours and is left alone. Returns False when it could not be done now
        (retried later from state['unverified'])."""
        try:
            cur = self.get_config()
            keep = list(cur.get(PATS) or [])
            for r in rules:
                own = [i for i, p in enumerate(keep)
                       if p["pattern"] == r["pattern"] and str(p.get("replacement") or "") == r["replacement"]]
                if own:
                    del keep[own[-1]]
            if len(keep) != len(cur.get(PATS) or []):
                self.api("PATCH", PROVIDER, {PATS: keep})
                if not same(self.get_config(), dict(cur, **{PATS: keep})):
                    self.disable_apply(f"撤回规则后读回的配置和预期不一致（{why}）",
                                       "备份在 state/backups/antigravity.*.json")
                    return False
        except ft.Abort as exc:
            log(f"撤回规则暂时没成功（{why}：{exc}），稍后自动重试")
            return False
        self.drop_unverified(rules)
        log(f"已撤回规则（{why}）")
        return True

    def recover(self):
        """Remove rules that were written but never verified (crash, CPA down during a rollback)."""
        if (not self.st.d["unverified"] or not self.mgmt_key or self.st.d["apply_disabled"]
                or time.time() < self.next_recover):
            return
        self.next_recover = time.time() + RECOVER_EVERY
        if not all(isinstance(u, list) and len(u) == 2 for u in self.st.d["unverified"]):
            self.disable_apply("状态里没验证的规则记录格式不认识（旧版本留下的？），不敢猜是哪条",
                               "人工核对 CPA 里的规则和 state/backups/ 后，清空 state.json 的 unverified 再 --resume")
            return
        rules = [{"pattern": p, "replacement": r} for p, r in self.st.d["unverified"]]
        if self.remove_rules(rules, "上次写入后没有验证完"):
            self.event("ROLLBACK", f"处理了 {len(rules)} 条写入后没验证过的规则：还是原样的已撤回，被人改过的保留",
                       "\n".join(r["pattern"] for r in rules))

    # ---- loop
    def resume_pending(self):
        now = time.time()
        if (self.stopped() or self.quarantined() or now < self.st.d["quiet_until"]
                or now - self.st.d["last_start"] < GAP_SECONDS):
            return
        for fp, item in list(self.st.d["pending"].items()):
            need = (1 if item.get("found") else 2) * self.cost
            if now >= item.get("retry_at", 0) and self.budget.left() >= need:
                item["retry_at"] = now + RETRY_PENDING      # never spin, whatever happens below
                self.st.d["last_start"] = now
                self.st.save()
                log(f"继续之前暂存的调查 {fp}")
                self.investigate(item)
                return

    def run(self):
        self.seen = self.logs()
        settled = self.logs(settled=True)
        self.tos_seen = set(settled)      # files still being written get their 403 check later
        cutoff = max(time.time() - 3600, self.st.d["resumed_at"])   # a 403 a human already resumed after stays handled
        if not self.a.dry_run:
            for name in settled:   # a 403 that arrived while the guard was down
                p = os.path.join(self.a.log_dir, name)
                if os.path.getmtime(p) > cutoff:
                    self.scan_tos(p)
        log(f"开始监听 {self.a.log_dir}，已有 {len(self.seen)} 个日志不处理。每个 429 算 {self.cost} 次上游命中，"
            f"预算 {self.a.hourly_budget}/小时、{self.a.daily_budget}/天，当前剩余 {self.budget.left()}。"
            + ("【试运行：不发请求、不改规则】" if self.a.dry_run else ""))
        while True:
            try:
                self.st.refresh_flags()
                if not self.a.dry_run:
                    self.recover()
                current = self.logs()
                self.seen &= current          # CPA deletes old logs; keep the sets small
                self.tos_seen &= current
                for name in sorted(self.logs(settled=True) - self.seen):
                    self.seen.add(name)
                    self.tos_seen.add(name)
                    self.handle(os.path.join(self.a.log_dir, name))
                if not self.a.dry_run:
                    self.resume_pending()
                    self.scan_new_logs()
            except Exception as exc:  # noqa: BLE001 - the daemon must survive anything
                log(f"出错（已忽略，继续监听）：{exc.__class__.__name__}: {exc}")
            time.sleep(self.a.poll)


def main():
    ap = argparse.ArgumentParser(description="自动处理反重力假 429（在 CPA 服务器上运行）")
    ap.add_argument("--cpa-dir", default="/root/cliproxyapi")
    ap.add_argument("--log-dir", default="/root/.cli-proxy-api/logs")
    ap.add_argument("--state-dir", default="/root/cpa-guard/state")
    ap.add_argument("--base", default="http://127.0.0.1:8317")
    ap.add_argument("--model-regex", default=r"^(gemini-|claude-|gpt-oss)", help="只处理这些模型的 429（反重力的模型）")
    ap.add_argument("--hourly-budget", type=int, default=10, help="每小时最多造成多少次上游 429 命中，默认 10")
    ap.add_argument("--daily-budget", type=int, default=24, help="每天最多造成多少次上游 429 命中，默认 24")
    ap.add_argument("--max-requests", type=int, default=30, help="单次调查最多发多少次请求，默认 30")
    ap.add_argument("--candidates", type=int, default=3)
    ap.add_argument("--pause", type=float, default=5.0)
    ap.add_argument("--cooldown-wait", type=int, default=70)
    ap.add_argument("--reload-wait", type=float, default=6.0, help="写入规则后等几秒再验证")
    ap.add_argument("--rewrite-model", default="gpt-6-luna", help="把触发行翻译成中文用的模型（必须不是反重力的模型，走 CPA 的别的渠道），默认 gpt-6-luna；给空字符串就不处理触发行，只报告")
    ap.add_argument("--poll", type=float, default=5.0)
    ap.add_argument("--notify-cmd", default="", help="通知命令（shell），环境变量 GUARD_KIND / GUARD_TITLE / GUARD_DETAIL")
    ap.add_argument("--no-apply", action="store_true", help="只报告，不改 CPA 的规则")
    ap.add_argument("--dry-run", action="store_true", help="只分析，不发任何请求，不改状态")
    ap.add_argument("--once", metavar="LOG", help="只处理这一个日志然后退出")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--test-notify", action="store_true", help="发一条测试通知（Bark / 邮件）然后退出")
    ap.add_argument("--resume", action="store_true", help="人工确认没事后，清除停止标志（403 之后、管理接口异常之后）")
    a = ap.parse_args()
    g = Guard(a)
    if a.test_notify:
        notify("TEST", "测试通知", "如果你看到这条，说明 cpa_guard 的通知能送到。")
        return 0
    if a.status:
        print(json.dumps({"halted": g.st.d["halted"], "apply_disabled": g.st.d["apply_disabled"],
                          "unverified_rules": g.st.d["unverified"], "budget_left": g.budget.left(),
                          "cost_per_429": g.cost, "used_1h": g.budget.used(3600), "used_24h": g.budget.used(DAY),
                          "pending": list(g.st.d["pending"]), "quiet_until": g.st.d["quiet_until"]},
                         ensure_ascii=False, indent=1))
        return 0
    if a.resume:
        g.st.refresh_flags()
        g.st.d["halted"] = g.st.d["apply_disabled"] = None
        g.st.d["resumed_at"] = time.time()
        g.st.save()
        print("已清除停止标志（halted / apply_disabled）。")
        return 0
    if not a.dry_run:
        if not g.api_key:
            print("找不到客户端密钥：设置环境变量 CPA_API_KEY，或确认 config.yaml 里有 api-keys。")
            return 2
        lock = open(os.path.join(a.state_dir, "lock"), "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("另一个 cpa_guard 正在运行（守护进程或另一个 --once），不要同时跑两个，预算会被重复花掉。")
            return 2
        g.recover()
    if a.once:
        if not a.dry_run:
            g.tos_seen = g.logs(settled=True)
        g.handle(a.once)
        return 0
    g.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
