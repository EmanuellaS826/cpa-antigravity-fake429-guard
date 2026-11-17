#!/usr/bin/env python3
"""Find the system-prompt text that makes Antigravity return a fake 429.

Run it ON THE CPA SERVER (it talks to CPA on 127.0.0.1). Only the Python 3
standard library is needed.

Typical use:
  python3 find_trigger.py --log /root/.cli-proxy-api/logs/error-xxxx.log --dry-run
  python3 find_trigger.py --log /root/.cli-proxy-api/logs/error-xxxx.log \
      --api-key sk-xxx --mgmt-key yyy

How it works (each 429 is a deliberate hit on your account, so it tries to
send as few as possible):
  1. Reads the client request saved in a CPA error log and extracts the
     system prompt (OpenAI Chat, OpenAI Responses, Claude and Gemini formats).
  2. Control: a neutral system prompt must return 200.
  3. Full system prompt. 200 = "not reproduced" (not proof the rules cover it).
  4. Suspect lines (lines new compared with --baseline, the first line, lines
     like "powered by" / "You are" / URLs / headings) are tested first: the
     prompt with all suspects removed, then the top suspects one by one.
  5. If no suspect hits, gallop (1, 2, 4... lines) then bisect for the shortest
     prefix, then the largest start. Word level only with --words.
  6. The result is confirmed with a fresh request (cache bypassed), and the
     prompt minus the result is tested to hint whether more triggers exist.
  7. 403 / TOS_VIOLATION stops at once. Anything inconclusive (502
     MALFORMED_FUNCTION_CALL, 503, odd status, a result that does not repeat)
     stops the run, clears cooldown and is never cached as a result.

Every test request goes through CPA, so CPA's current filter rules are applied
to it exactly as they are applied to real traffic.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

NEUTRAL_SYSTEM = "You are a helpful assistant."
PROBE_UA = "cpa-trigger-probe/1"
USER_MESSAGE = "Reply with exactly: hello"
MGMT = "/v8/management"   # CPA 8.0.76: /v8/management/credentials and /routing/cooldown/reset both verified


class Abort(Exception):
    pass


class LimitReached(Exception):
    pass


class TosViolation(Abort):
    pass


# ---------------------------------------------------------------- log parsing

def read_log(path):
    """Return (url, body_dict) from a CPA error/request log."""
    lines = open(path, encoding="utf-8", errors="replace").read().split("\n")
    url = ""
    for line in lines:
        if line.startswith("URL: "):
            url = line[5:].strip()
            break
    body_lines = []
    in_body = False
    for line in lines:
        if line.startswith("=== REQUEST BODY ==="):
            in_body = True
            continue
        if in_body:
            if line.startswith("=== "):
                break
            body_lines.append(line)
    raw = "\n".join(body_lines).strip()
    if not raw:
        raise Abort("日志里没有找到 '=== REQUEST BODY ===' 段落，换一个 error-*.log 文件试试。")
    try:
        return url, json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Abort(f"REQUEST BODY 不是合法 JSON（{exc}）。可能是带图片的超大请求被截断了，换一个日志。")


def texts_of(content):
    """Collect text from a string / list-of-parts / dict content value."""
    if content is None:
        return []
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        out = []
        for item in content:
            out.extend(texts_of(item))
        return out
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return [content["text"]]
        for key in ("content", "parts"):
            if key in content:
                return texts_of(content[key])
    return []


def extract_system(body):
    """Return the system prompt text of a client request in any supported format."""
    parts = []
    # OpenAI Chat Completions: messages[].role == system/developer
    for msg in body.get("messages") or []:
        if isinstance(msg, dict) and str(msg.get("role", "")).lower() in ("system", "developer"):
            parts.extend(texts_of(msg.get("content")))
    # OpenAI Responses: instructions + input[].role == system/developer
    if isinstance(body.get("instructions"), str):
        parts.append(body["instructions"])
    inputs = body.get("input")
    for item in inputs if isinstance(inputs, list) else []:
        if isinstance(item, dict) and str(item.get("role", "")).lower() in ("system", "developer"):
            parts.extend(texts_of(item.get("content")))
    # Claude Messages: system (string or list of blocks)
    if "system" in body:
        parts.extend(texts_of(body["system"]))
    # Gemini: systemInstruction / system_instruction
    for key in ("systemInstruction", "system_instruction"):
        if key in body:
            parts.extend(texts_of(body[key]))
    return "\n\n".join(p for p in parts if p)


def model_of(url, body, override):
    if override:
        return override
    if isinstance(body.get("model"), str) and body["model"]:
        return body["model"]
    match = re.search(r"/models/([^/:?]+)", url or "")
    if match:
        return match.group(1)
    return "gemini-3.8-flash-high"


# ------------------------------------------------------------------ requester

class Tester:
    def __init__(self, base, api_key, mgmt_key, model, max_requests, pause, cooldown_wait):
        self.base = base.rstrip("/")
        self.api_key = api_key
        self.mgmt_key = mgmt_key
        self.model = model
        self.max_requests = max_requests
        self.pause = pause
        self.cooldown_wait = cooldown_wait
        self.sent = 0
        self.cache = {}
        self.last_trigger = ""
        self.shown_429 = False
        self.gate = None     # optional callable, called before every request; may raise to stop
        self.on_429 = None   # optional callable, called after every upstream 429
        self.found_so_far = []       # set by search(): triggers confirmed so far
        self.rest_unchecked = False  # search() was interrupted while checking the prompt minus found_so_far

    def _post(self, path, payload, headers):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            raise Abort(f"连不上 CPA（{self.base}）：{exc.reason}。确认脚本是在 CPA 服务器上运行，--base 地址正确。")
        except OSError as exc:  # timeouts, connection resets
            raise Abort(f"请求 CPA 时出错（{exc.__class__.__name__}: {exc}）。结果不确定，没有记入结果。")

    def reset_cooldown(self):
        if not self.mgmt_key:
            print(f"    （没有提供 --mgmt-key，等 {self.cooldown_wait} 秒让账号冷却结束）", flush=True)
            time.sleep(self.cooldown_wait)
            return
        headers = {"Authorization": "Bearer " + self.mgmt_key, "Content-Type": "application/json"}
        indexes = self._antigravity_auth_indexes(headers)
        ok = bool(indexes)
        for index in indexes:
            code, _ = self._post(MGMT + "/routing/cooldown/reset", {"auth_index": index}, headers)
            ok = ok and code < 400
        if not ok:
            print(f"    （清除冷却失败；改为等 {self.cooldown_wait} 秒）", flush=True)
            time.sleep(self.cooldown_wait)

    def _antigravity_auth_indexes(self, headers):
        """auth_index of every enabled Antigravity credential (the reset API works per credential)."""
        req = urllib.request.Request(self.base + MGMT + "/credentials", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                files = json.loads(resp.read().decode("utf-8", "replace")).get("files") or []
        except urllib.error.HTTPError as exc:
            print(f"    （读账号列表失败：管理接口返回 HTTP {exc.code}）", flush=True)
            return []
        except (OSError, ValueError) as exc:
            print(f"    （读账号列表失败：{exc.__class__.__name__}）", flush=True)
            return []
        return [f["auth_index"] for f in files
                if f.get("provider") == "antigravity" and not f.get("disabled") and f.get("auth_index")]

    def triggers(self, system_text, label, fresh=False, reset_on_429=True):
        """True = upstream fake 429, False = 200. fresh=True bypasses the cache.
        Raises Abort for 403 and for anything inconclusive (never cached)."""
        key = hashlib.sha256(system_text.encode("utf-8")).hexdigest()
        if not fresh and key in self.cache:
            return self.cache[key]
        for attempt in range(3):
            if self.sent >= self.max_requests:
                raise LimitReached()
            if self.gate:
                self.gate()
            self.sent += 1
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_text},
                    {"role": "user", "content": USER_MESSAGE},
                ],
                "max_tokens": 256,
                "stream": False,
            }
            headers = {"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json",
                       "User-Agent": PROBE_UA}
            started = time.time()
            code, text = self._post("/v1/chat/completions", payload, headers)
            took = time.time() - started
            short = len(system_text)
            if code == 403 or "TOS_VIOLATION" in text:
                raise TosViolation("收到 403 / TOS_VIOLATION：这个账号被谷歌封了，不是 429 问题。立刻停止测试，换号或去申诉。")
            if code == 200:
                print(f"  [{self.sent:>2}] 200 通过  {took:5.1f}s  {short:>6} 字符  {label}", flush=True)
                self.cache[key] = False
                time.sleep(self.pause)
                return False
            if code == 429 and "cooling down" in text:
                print(f"  [{self.sent:>2}] 账号还在冷却，不算结果，清除冷却后重发", flush=True)
                self.reset_cooldown()
                continue
            if code == 429:
                print(f"  [{self.sent:>2}] 429 触发  {took:5.1f}s  {short:>6} 字符  {label}", flush=True)
                if not self.shown_429:
                    self.shown_429 = True
                    print("       上游回应：" + " ".join(text.split())[:240], flush=True)
                self.cache[key] = True
                if self.on_429:
                    self.on_429()
                self.last_trigger = system_text
                if reset_on_429:
                    self.reset_cooldown()
                time.sleep(self.pause)
                return True
            if code in (401, 402):
                raise Abort(f"HTTP {code}：--api-key 不对（要用 CPA 配置里 api-keys 下的客户端密钥）。")
            # Everything below is inconclusive: not a pass, not a trigger. Do not cache.
            self.reset_cooldown()
            if code == 502 and "MALFORMED_FUNCTION_CALL" in text:
                raise Abort("HTTP 502 MALFORMED_FUNCTION_CALL：模型生成了坏的工具调用，这个结果既不能算通过也不能算触发。"
                            "已清除冷却，没有记入结果。换一段文字重试，或用真实请求重放。")
            if code == 503 or "auth_unavailable" in text or "no auth available" in text:
                raise Abort("HTTP 503 没有可用账号：已清除冷却。确认有一个反重力账号是启用状态，等一会再测。")
            raise Abort(f"意外的回应 HTTP {code}（结果不确定，没有记入结果，已清除冷却）：{text[:300]}")
        raise Abort("账号一直在冷却，清不掉。等几分钟再运行。")

    def confirm(self, text, label):
        """Re-send text bypassing the cache; it must trigger again, otherwise the earlier 429 was probably transient."""
        if not self.triggers(text, label, fresh=True):
            raise Abort("复测没有再触发 429：之前的 429 很可能是临时限流，不是这段文字造成的。"
                        "结果不可靠，没有输出触发句。过几分钟再运行一次。")


# ------------------------------------------------------------------ narrowing

def shortest_prefix(units, triggers):
    """Smallest j such that units[:j] triggers (units[:len] is known to trigger).

    Probes 1, 2, 4, 8 lines first (a trigger near the top costs 1-4 requests instead of
    log2(n) deliberate 429s), then bisects the rest. Galloping further would cost more
    than plain bisection for a trigger deep in the prompt."""
    n = len(units)
    lo, hi = 1, n
    for size in (1, 2, 4, 8):
        if size >= n:
            break
        if triggers("".join(units[:size])):
            hi = size
            break
        lo = size + 1
    while lo < hi:
        mid = (lo + hi) // 2
        if triggers("".join(units[:mid])):
            hi = mid
        else:
            lo = mid + 1
    return lo


def largest_start(units, end, triggers):
    """Largest i such that units[i:end] triggers (units[0:end] is known to trigger)."""
    lo, hi = 0, end - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if triggers("".join(units[mid:end])):
            lo = mid
        else:
            hi = mid - 1
    return lo


def narrow(units, tester, what):
    """Return (start, end) such that units[start:end] triggers."""
    print(f"\n按{what}缩小范围（共 {len(units)} 个）……", flush=True)
    end = shortest_prefix(units, lambda t: tester.triggers(t, f"前 N 个{what}"))
    start = largest_start(units, end, lambda t: tester.triggers(t, f"第 i 到第 {end} 个{what}"))
    return start, end


def ddmin(units, triggers):
    """Smallest subset of units (as indexes) that still triggers; units as a whole is known to trigger.

    Delta debugging (idea from cpa-sentinel's bisect.rs): also finds triggers that need several
    lines at once. units is the window that narrow() returned, so its first and last line are
    already known to be needed (shortest prefix / largest start); they stay fixed and only the
    lines in between are searched. A pair of lines far apart then costs 1 request, not dozens."""
    last = len(units) - 1
    def hit(inner):
        return triggers("".join(units[i] for i in sorted([0, last] + inner)))
    cur = list(range(1, last))
    if not cur or hit([]):
        return [0, last]
    chunks = 2
    while len(cur) > 1:
        size = -(-len(cur) // chunks)
        parts = [cur[i:i + size] for i in range(0, len(cur), size)]
        reduced = False
        for part in parts:
            if hit(part):
                cur, chunks, reduced = part, 2, True
                break
        if reduced:
            continue
        if len(parts) > 2:
            for skip in range(len(parts)):
                comp = [i for k, p in enumerate(parts) if k != skip for i in p]
                if hit(comp):
                    cur, chunks, reduced = comp, max(chunks - 1, 2), True
                    break
            if reduced:
                continue
        if chunks >= len(cur):
            break
        chunks = min(chunks * 2, len(cur))
    return sorted([0, last] + cur)


SUSPECT_RE = re.compile(r"powered by|you are\b|built (on|by|with)|created by|developed by|https?://|^\s*#{1,6}\s", re.I)


def pick_suspects(lines, baseline_lines, cap=12):
    """Indexes of lines worth testing first, most likely first. Costs no request."""
    known = {l.strip() for l in baseline_lines}
    order = []
    def add(i):
        if i not in order and lines[i].strip():
            order.append(i)
    if known:
        for i, l in enumerate(lines):
            if l.strip() and l.strip() not in known:
                add(i)
    for i, l in enumerate(lines):
        if l.strip():
            add(i)  # first non-empty line
            break
    for i, l in enumerate(lines):
        if SUSPECT_RE.search(l):
            add(i)
    return order[:cap]


def without(lines, drop):
    return "".join(l for i, l in enumerate(lines) if i not in drop)


def search(tester, lines, baseline_lines, candidates=3, rounds=3, words=False, assume_full=False):
    """Find the trigger lines of a system prompt (split with keepends). Returns (found, more).

    found = [(1-based line numbers, text)], more = True when the prompt minus the found parts
    still triggers. The caller has already shown that the full prompt triggers, or passes
    assume_full=True when the log itself is the evidence (saves one 429); then an all-200
    outcome raises Abort (not reproduced / transient) instead of a wrong answer."""
    found = []
    tester.found_so_far = found      # progress for a caller that is interrupted mid-search
    tester.rest_unchecked = False
    cur = list(range(len(lines)))   # lines still in the prompt (original index)
    more = True
    for rnd in range(1, rounds + 1):
        cur_lines = [lines[i] for i in cur]
        if rnd > 1:
            print(f"\n===== 第 {rnd} 处触发句 =====", flush=True)
        window = None
        if len(cur_lines) == 1:
            window = (0, 1)
        else:
            suspects = pick_suspects(cur_lines, baseline_lines) if rnd == 1 else []
            if suspects:
                print(f"\n先查嫌疑行（共 {len(suspects)} 行：{', '.join(str(cur[i] + 1) for i in suspects)}）……", flush=True)
                rest = without(cur_lines, set(suspects))
                if not rest.strip():
                    pass
                elif tester.triggers(rest, "去掉全部嫌疑行的完整提示"):
                    print("  去掉嫌疑行后仍然 429：触发句不在嫌疑行里，改用范围搜索。", flush=True)
                else:
                    for i in suspects[:candidates]:
                        if tester.triggers(cur_lines[i], f"嫌疑行 {cur[i] + 1}"):
                            window = (i, i + 1)
                            break
                    if window is None:
                        if assume_full:
                            print("  单独测的嫌疑行都没触发；再确认一次完整提示还会不会 429 ……", flush=True)
                            if not tester.triggers("".join(cur_lines), "完整提示（确认）"):
                                raise Abort("完整提示这次返回 200：之前的 429 很可能是临时限流。没有找到触发句，没有加规则。")
                        else:
                            print("  单独测的嫌疑行都没触发，改用范围搜索。", flush=True)
            if window is None:
                window = narrow(cur_lines, tester, "行")

        start, end = window
        idx = list(range(start, end))
        if len(idx) > 1:
            print(f"\n范围里有 {len(idx)} 行，去掉没用的行（可能要几行一起才触发）……", flush=True)
            sub = ddmin(cur_lines[start:end], lambda t: tester.triggers(t, "范围内的子集"))
            idx = [start + i for i in sub]
        phrase = "".join(cur_lines[i] for i in idx)
        if words and len(idx) == 1:
            tokens = re.findall(r"\s*\S+", phrase)
            if len(tokens) > 1:
                ws, we = narrow(tokens, tester, "词")
                phrase = "".join(tokens[ws:we])

        print("\n复测（绕过缓存重新发一次）……", flush=True)
        tester.confirm(phrase, "复测触发句")
        found.append(([cur[i] + 1 for i in idx], phrase))

        drop = set(idx)
        cur = [c for j, c in enumerate(cur) if j not in drop]
        residual = "".join(lines[i] for i in cur)
        tester.last_trigger = residual   # where a resumed search has to continue
        tester.rest_unchecked = True
        print("\n去掉它以后，完整提示还会不会 429？", flush=True)
        more = tester.triggers(residual, "去掉已找到的触发句后的完整提示") if residual.strip() else False
        tester.rest_unchecked = False
        if not more:
            tester.last_trigger = ""
            break
    return found, more


# ----------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="找出让反重力返回假 429 的系统提示片段（在 CPA 服务器上运行）")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--log", help="CPA 的 error-*.log 文件路径")
    src.add_argument("--text-file", help="直接给一个纯文本文件作为系统提示")
    ap.add_argument("--base", default="http://127.0.0.1:8317", help="CPA 地址，默认 http://127.0.0.1:8317")
    ap.add_argument("--api-key", default=os.environ.get("CPA_API_KEY", ""), help="CPA 客户端密钥（config.yaml 里 api-keys 下的一个），也可用环境变量 CPA_API_KEY")
    ap.add_argument("--mgmt-key", default=os.environ.get("CPA_MGMT_KEY", ""), help="CPA 管理密钥，用来在每次 429 后清除冷却；也可用环境变量 CPA_MGMT_KEY。不给就每次 429 后干等")
    ap.add_argument("--model", default="", help="测试用的模型，默认取日志里的模型")
    ap.add_argument("--baseline", help="同一个 agent 上一次能通过的系统提示（纯文本文件或 error-*.log）。新增的行会被优先怀疑，不用发请求就能缩小范围")
    ap.add_argument("--candidates", type=int, default=3, help="嫌疑行最多单独测几行，默认 3")
    ap.add_argument("--words", action="store_true", help="找到触发行后继续缩小到词（多花十来次 429，一般不需要）")
    ap.add_argument("--max-requests", type=int, default=25, help="最多发多少次请求，默认 25")
    ap.add_argument("--rounds", type=int, default=3, help="最多连续找几处触发句，默认 3（找到一处就在本地去掉它，接着找下一处）")
    ap.add_argument("--pause", type=float, default=3.0, help="两次请求之间等几秒，默认 3")
    ap.add_argument("--cooldown-wait", type=int, default=70, help="没有管理密钥时，每次 429 后等几秒，默认 70")
    ap.add_argument("--dry-run", action="store_true", help="只显示提取到的系统提示，不发请求")
    args = ap.parse_args()

    try:
        if args.log:
            url, body = read_log(args.log)
            system = extract_system(body)
            model = model_of(url, body, args.model)
        else:
            url, body = "", {}
            system = open(args.text_file, encoding="utf-8").read()
            model = model_of("", {}, args.model)

        if not system.strip():
            raise Abort("这条请求没有系统提示。假 429 只看系统提示，这条请求的 429 可能是真的额度用完或别的原因。")

        lines = system.splitlines(keepends=True)
        print(f"来源接口：{url or '(文本文件)'}")
        print(f"测试模型：{model}")
        print(f"系统提示：{len(system)} 字符，{len(lines)} 行")
        print("开头 300 字符：")
        print("  " + system[:300].replace("\n", "\n  "))

        if args.dry_run:
            print("\n--dry-run：没有发送任何请求。")
            return 0
        if not args.api_key:
            raise Abort("缺少 --api-key（或环境变量 CPA_API_KEY）。")

        tester = Tester(args.base, args.api_key, args.mgmt_key, model,
                        args.max_requests, args.pause, args.cooldown_wait)

        print("\n第 1 步：对照组（普通系统提示），应该返回 200 ……", flush=True)
        if tester.triggers(NEUTRAL_SYSTEM, "对照组"):
            raise Abort("对照组也是 429：账号可能真的没额度了，或者被限流。先别测，过一段时间再试。")

        print("\n第 2 步：完整系统提示 ……", flush=True)
        if not tester.triggers(system, "完整系统提示"):
            print("\n结果：这次完整系统提示没有触发 429（未复现）。")
            print("可能是：现有规则已经覆盖（刚加完规则时，这就是验证通过）；也可能原来的 429 是临时限流，或和请求里的")
            print("其他内容有关（本脚本不带工具和聊天记录）。真实客户端还是 429 的话：先看是不是真没额度、账号被封（403），")
            print("再把日志里的完整请求原样重放一次。")
            return 0

        baseline_lines = []
        if args.baseline:
            if args.baseline.endswith(".log"):
                baseline_lines = extract_system(read_log(args.baseline)[1]).splitlines()
            else:
                baseline_lines = open(args.baseline, encoding="utf-8").read().splitlines()
        found, more = search(tester, lines, baseline_lines, args.candidates, args.rounds, args.words)

        print("\n" + "=" * 60)
        print(f"找到 {len(found)} 处（每处单独或合在一起作为系统提示就会触发 429，已复测）")
        print("=" * 60)
        for n, (nums, text) in enumerate(found, 1):
            print(f"[{n}] 原提示第 {', '.join(map(str, nums))} 行：")
            print("    " + text.strip().replace("\n", "\n    "))
        print("=" * 60)
        print(f"共发送 {tester.sent} 次请求。")
        if more:
            print(f"注意：去掉这 {len(found)} 处以后完整提示还是 429（已到 --rounds 上限），还有别的触发句。先照手册第 6 节加规则，")
            print("再用同一个日志重跑本脚本找下一处（规则一变，测试结果就和以前不同，不要沿用旧结论）。")
        else:
            print("去掉它们以后完整提示返回 200：大概率就这些（只是线索，不是证明）。")
            print("下一步：照手册第 6 节写成替换规则，再用同一个日志重跑本脚本验证（第 2 步显示'未复现'就是成功）。")
        return 0
    except LimitReached:
        print(f"\n已达到 --max-requests 上限（{args.max_requests} 次），停止。")
        out = "find_trigger_last.txt"
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(tester.last_trigger)
        print(f"目前找到的最短触发文字（{len(tester.last_trigger)} 字符）已存到 {out}。")
        print(f"过一段时间再接着找：python3 find_trigger.py --text-file {out} 加上原来的密钥参数。")
        return 2
    except Abort as exc:
        print(f"\n停止：{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
