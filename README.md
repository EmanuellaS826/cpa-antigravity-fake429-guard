# CPA 反重力（Antigravity）"假 429" 处理手册

整理日期：2026-11-17。适用：CLIProxyAPI（下称 CPA）8.0.72～8.0.78（8.0.77、8.0.78 只确认了规则还在、服务正常，没重测触发句），用反重力 OAuth 账号对外提供 Gemini 等模型，接 Hermes、AstrBot、omp、Claude Code、Codex 这类 agent 产品。

这份手册可以直接分享给朋友，里面没有任何密钥、IP 或个人信息。给 AI 维护者看的硬性规则在第 9 节，**AI 动手之前必须先读第 9 节**。

本仓库里的文件：

| 文件 | 用途 |
| --- | --- |
| `README.md` | 本手册 |
| `find_trigger.py` | 手动找触发句的脚本（第 6 节），只需要 Python 3，不用装任何东西 |
| `cpa_guard.py` | 常驻服务器、自动处理假 429 的守护进程（第 11 节），需要和 `find_trigger.py` 放在同一个文件夹 |
| `REVIEW.md` | `cpa_guard.py` 的设计取舍、三轮代码审查的结论和测试记录 |
| `tests/` | 假 CPA 和回归测试脚本，不碰真实账号（第 11 节"自己跑测试"） |

---

## 1. 这是什么问题

**症状**：通过 CPA 用反重力的模型，某些 agent 一直报：

```text
429 upstream execution failed: provider=antigravity model=gemini-3.8-flash-high ...
{"error":{"code":429,"message":"Resource has been exhausted (e.g. check quota).","status":"RESOURCE_EXHAUSTED"}}
```

接着 CPA 还会报 `All credentials for model ... are cooling down`。

**真相**：额度根本没用完。谷歌会检查发给它的**系统提示**（system prompt），发现第三方 agent 的特征文字（比如产品名、某个固定标题、某句固定开场白），就拒绝请求，但故意把理由写成"额度用完"。同一个账号在官方 Antigravity 客户端里照常能用。

**连带后果**：CPA 以为额度真用完了，会让账号进入冷却。冷却期间**所有人**用这个模型都失败，不只是触发的那个 agent。

### 1.1 先分清是哪一种错误

| 你看到的 | 是什么 | 怎么办 |
| --- | --- | --- |
| 429 `RESOURCE_EXHAUSTED`；同一账号发一句"你好"（没有系统提示，或系统提示很普通）能成功，换某个 agent 就失败 | **假 429**（本手册要解决的） | 第 5、6 节 |
| 429；普通的一句"你好"也失败；面板里额度显示真用完了 | 真没额度 | 等额度恢复，或加账号 |
| 403 `PERMISSION_DENIED`，消息里有 `violation of Terms of Service` / `TOS_VIOLATION` | **账号被谷歌封了** | 改提示词没用。停止用这个号，去申诉（第 8 节） |
| 429 `All credentials ... are cooling down` | CPA 自己在冷却账号（通常是前面的假 429 引起的） | 先解决假 429；急用可以在面板里重置冷却 |

### 1.2 已经搞清楚的规律（实测）

- 谷歌**只检查系统提示**。工具定义、聊天记录、用户消息里出现同样的字眼都不会触发。
- 是**按原文匹配**：同一句话换一个词、删一个逗号、调换顺序，往往就能通过。所以谷歌换规则，原来能通过的写法也可能变成不能通过。
- 插入看不见的字符（零宽字符、U+034F）**已经不管用**了：2026-11-15 实测 `AstrBot` 加了这种字符照样 429。必须**整个换掉**或**删掉**。
- 不是只要出现"第三方产品名"就拦。拦的是谷歌名单上的具体文字，名单随时会变。

---

## 2. 解决思路

CPA 自带两种过滤规则，**只改发往反重力的系统提示**，其他内容（工具、聊天记录、用户消息）不动，也不影响 Codex 等其他渠道：

| 配置项 | 做什么 | 适合 |
| --- | --- | --- |
| `sensitive-words-replace` | 原文替换，不区分大小写，长的词优先 | 产品名、网址这类固定文字 |
| `sensitive-patterns-replace` | 正则替换（Go 的 RE2 语法），**先于**原文替换执行；替换成 `""` 就是删掉 | 整行删除、只匹配完整单词、区分大小写、改写固定句式 |

位置都在 `config.yaml` 的 `oauth.providers.antigravity` 下面。

**不要用这些：**

| 别用 | 为什么 |
| --- | --- |
| `sensitive-words`（网页面板可视化页面里叫"敏感词混淆"） | 只插入看不见的字符，实测已经骗不过谷歌 |
| 插件市场的 `antigravity-prompt-filter` 插件 | 它的词表已经全部搬进下面的配置了。它是简单的查找替换，会把 decline（拒绝）改成 deAntigravity，把 `D:\...\hermes\...` 这类路径改错，模型就会写错文件。插件还是第三方编译的程序，CPA 升级后可能失效 |
| 改 agent 那边的提示词 | 每个 agent、每台电脑都得改一遍，agent 一升级又被改回去。在 CPA 上统一处理，所有接入的 agent 一起生效 |

**代价**：这些产品名都会被换成 `Antigravity`，模型会以为自己叫 Antigravity。这是换来不被拦的代价，对日常使用基本没影响。

---

## 3. 推荐配置（可以直接复制）

把下面这段放进 `config.yaml`。如果 `oauth:` → `providers:` → `antigravity:` 已经存在（比如里面已经有 `antigravity-credits: true`），**只把 `antigravity:` 下面的两组规则加进去**，不要再写一个 `oauth:`，否则 YAML 会出错或互相覆盖。

```yaml
oauth:
    providers:
        antigravity:
            # 反重力"假 429"过滤规则，只改发往谷歌的系统提示。说明见《CPA 反重力假 429 处理手册》。
            # 正则规则：先执行；用于整行删除、需要单词边界的短名字、改写固定句式
            sensitive-patterns-replace:
                # Hermes 用 Gemini 时自动加的标题，删掉这一行，下面的规则正文保留
                - pattern: '(?m)^#{1,6}[ \t]*Google model operational directives[ \t]*\r?\n?'
                  replacement: ""
                # omp 的 "RFC 2119 keywords: MUST, ..." / 旧版 "RFC 2119: MUST, ..."，意思不变
                - pattern: '(?i)\bRFC 2119( keywords)?:'
                  replacement: "Requirement keywords:"
                # 短名字或常见单词的一部分，必须按整词匹配（避免 decline、raider 被改坏）
                - pattern: '(?i)\b(Cline|Aider|Goose|Kiro|Trae|Tabby|Devin|Qodo|Qoder CLI|Qoder)\b'
                  replacement: "Antigravity"
                # cursor 也是普通编程词，只换大写开头的产品名
                - pattern: '\bCursor\b'
                  replacement: "Antigravity"
                # 大写开头的 Hermes 产品名；小写的 hermes-agent 技能名和路径不动，技能还能正常加载
                - pattern: '\bHermes\b'
                  replacement: "Antigravity"
            # 原文替换：不区分大小写，长的优先匹配
            sensitive-words-replace:
                # 已实测触发
                "AstrBot": "Antigravity"
                # Hermes 系统提示里的文档网址也会触发（小写技能名 hermes-agent 和 D:\...\hermes 路径实测不触发，保留原样）
                "hermes-agent.nousresearch.com": "antigravity.nousresearch.com"
                # 安卓聊天 App 记忆功能说明里的一句（2026-11-15 实测），改成同义说法
                "A fact goes in the file for the domain it belongs to, not whichever file happens to be open.": "Put each fact in the file for its own topic, not in whichever file is currently open."
                # 社区实测过的触发词
                "Nous Research": "Antigravity"
                "Claude Agent SDK": "agent SDK"
                "system-conventions": "conventions"
                "system_conventions": "conventions"
                "system-directive": "directive"
                "system_directive": "directive"
                # 其他 agent 产品名（来自 antigravity-prompt-filter 插件的默认词表）
                "Claude Code": "Antigravity"
                "OpenAI Codex": "Antigravity"
                "Codex CLI": "Antigravity"
                "Codex": "Antigravity"
                "OpenCode": "Antigravity"
                "GitHub Copilot CLI": "Antigravity"
                "GitHub Copilot": "Antigravity"
                "Gemini Code Assist": "Antigravity"
                "Gemini CLI": "Antigravity"
                "Windsurf": "Antigravity"
                "Codeium": "Antigravity"
                "Roo Code": "Antigravity"
                "Kilo Code": "Antigravity"
                "Continue.dev": "Antigravity"
                "Amazon Q Developer": "Antigravity"
                "Amazon CodeWhisperer": "Antigravity"
                "JetBrains AI Assistant": "Antigravity"
                "JetBrains Junie": "Antigravity"
                "Qwen Code": "Antigravity"
                "Tabnine": "Antigravity"
                "Sourcegraph Cody": "Antigravity"
                "Augment Code": "Antigravity"
                "Replit Agent": "Antigravity"
                "Replit Ghostwriter": "Antigravity"
                "OpenHands": "Antigravity"
                "SWE-agent": "Antigravity"
                "Zed AI": "Antigravity"
                "Void Editor": "Antigravity"
                "PearAI": "Antigravity"
                "Refact.ai": "Antigravity"
                "GitLab Duo": "Antigravity"
                "Visual Studio IntelliCode": "Antigravity"
                "CodeBuddy": "Antigravity"
                "WorkBuddy": "Antigravity"
                "Blackbox AI": "Antigravity"
                "Pieces for Developers": "Antigravity"
                "CodiumAI": "Antigravity"
                "Rovo Dev CLI": "Antigravity"
                "Factory Droid": "Antigravity"
                "OpenClaw": "Antigravity"
                "Clawdbot": "Antigravity"
                "Moltbot": "Antigravity"

    # 建议同时加：让失败的请求完整留在日志里，第 6 节找触发句要用
observability:
    logs:
        logs-max-total-size-mb: 50
        error-logs-max-files: 300
```

注意：
- 缩进是 4 个空格一级，**不能用 Tab**。
- `observability:` 那段也可能已经存在（比如里面已经有 `debug: false`），同样只改这两个值，不要重复写 `observability:`。默认值 `logs-max-total-size-mb` 可能很小（例如 1），日志一写就被删，排查时就看不到失败请求。
- 老版本 CPA（7.x）的配置没有 `oauth.providers` 这一层，反重力设置直接写在顶层的 `antigravity:` 下。不确定的话，看你那个版本自带的 `config.example.yaml` 里 `sensitive-words-replace` 写在哪一层。7.x 早期版本可能没有 `sensitive-words-replace` / `sensitive-patterns-replace`，建议先升级 CPA。

### 3.1 这份配置在 2026-11-15 实测过的结果

用同一个反重力账号、`gemini-3.8-flash-high`：

| 测试内容 | 不加规则 | 加规则后 |
| --- | --- | --- |
| Hermes 完整系统提示 | 429 | 200 |
| Hermes 真实对话请求（225 条历史 + 21 个工具） | 429 | 200 |
| AstrBot 真实请求 | 429 | 200 |
| 安卓聊天 App（带记忆功能）真实请求（5.8MB，带 3 张图） | 429 | 200 |
| omp `RFC 2119 keywords: MUST, ...` | 社区报告 429 | 200 |
| omp 旧版 `<system-conventions>` + `RFC 2119: ...` | 社区报告 429 | 200 |
| Claude Code 子代理身份句 | 社区报告 429 | 200 |
| Codex 身份句 `You are Codex, an agent based on GPT-5.` | 社区报告 429 | 200 |

---

## 4. 在网页面板里看和改

1. 打开 CPA 管理页 `https://你的域名/management.html`，进"配置面板"。
2. 页面上方"编辑模式"从"可视化"切到"**源码**"。上面两组规则**只在源码模式里能看到**，可视化页面没有对应的输入框。可视化页面"高级设置 → antigravity"里的"敏感词混淆"是 `sensitive-words`，不要往里填（见第 2 节）。
3. 搜索 `sensitive-words-replace` 就能找到。
4. 改完点保存。CPA 会自动读取新配置，**不用重启**。YAML 写错了会提示"YAML 语法错误"，没保存成功之前 CPA 继续用旧配置。
5. 确认生效：面板日志（或服务器上 `logs/main.log`）里会出现这样的行：
   ```text
   config changes detected:
     antigravity.sensitive-words-replace: 49 -> 50
   ```

也可以直接在服务器上编辑 `config.yaml`，效果一样。**改之前先备份**：`cp config.yaml config.yaml.bak-$(date +%Y%m%d)`。

---

## 5. 日常：接了新 agent 怎么做

1. 先用这个 agent 正常聊一句。
2. 不报错：什么都不用做。
3. 报 429：先按第 1.1 节确认是**假 429**，再按第 6 节找触发句、加规则。
4. agent 升级以后也照这样聊一句检查。新版本可能改了系统提示的措辞，出现新的触发句。

---

## 6. 出现新的假 429：找触发句、加规则（一步一步来）

整个过程大约发 15～25 次测试请求，其中一半左右会故意触发 429。用的是你自己的账号，所以**能少发就少发**；每一步都按顺序来，不要跳步。

### 6.1 准备

1. 确认日志设置已经按第 3 节改成 `logs-max-total-size-mb: 50`、`error-logs-max-files: 300`。
2. 用出问题的 agent 再发一条消息，让它报一次 429。
3. 在 CPA 服务器上找到这次失败的日志。日志目录是 CPA 的 `auth-dir` 下的 `logs/`（常见是 `~/.cli-proxy-api/logs/`）：
   ```bash
   ls -t ~/.cli-proxy-api/logs/error-*.log | head -5
   ```
   时间最新、而且是这个 agent 发出的那个就是。分辨方法：打开文件，看 `=== HEADERS ===` 里的 `User-Agent`（例如 `astrbot/4.29.4`），或者 `=== REQUEST BODY ===` 里系统提示的开头。Hindsight 这类后台程序也会产生很多 error 日志，别拿错了。

### 6.2 用脚本自动找（推荐）

需要两个密钥，都**不要贴到聊天里**：
- **API 密钥**（`--api-key`）：CPA `config.yaml` 里 `api-keys` 列表下的任意一个客户端密钥（8.x 在 `access:` → `api-keys:` 下面），也就是 agent 连 CPA 时用的那个。
- **管理密钥**（`--mgmt-key`）：登录 CPA 网页面板用的那个密码。脚本用它在每次 429 后清除冷却。不提供也能跑，只是每次 429 后要干等 70 秒。

在 CPA 服务器上：

```bash
# 1. 把 find_trigger.py 传到服务器，比如 /root/find_trigger.py

# 2. 先只看提取到的系统提示，不发请求
python3 /root/find_trigger.py --log ~/.cli-proxy-api/logs/error-xxxx.log --dry-run

# 3. 正式找。密钥用环境变量传，命令历史里就不会留下
export CPA_API_KEY='这里填 API 密钥'
export CPA_MGMT_KEY='这里填管理密钥'
python3 /root/find_trigger.py --log ~/.cli-proxy-api/logs/error-xxxx.log
```

脚本会这样跑（每个 429 都是对你账号的一次"故意触发"，所以尽量少发）：
1. **对照组**：发一个普通系统提示，应该是 200。如果也是 429，说明账号真没额度或被限流，脚本会停下，不要继续测。
2. **完整系统提示**：经过 CPA 现有规则处理后再发。如果是 200，脚本报告"未复现"并停下。这**不等于**规则已经覆盖：也可能原来的 429 是临时限流，或和工具、聊天记录有关（脚本不带这些）。
3. **先查嫌疑行**（不用发请求就能挑出来）：新增行（给了 `--baseline` 时）、第一行、含 `powered by`、`You are`、`built by`、网址、`#` 标题的行。先发"去掉全部嫌疑行的完整提示"，返回 200 说明触发句在嫌疑行里，再把最可能的几行（`--candidates`，默认 3）单独测。
4. **嫌疑行没命中，才做范围搜索**：先试前 1、2、4、8 行，再二分，找出最短前缀，再找最大起点。触发句靠前时只要几次请求。默认**不再缩到词**（要的话加 `--words`，会多十来次 429）。
5. **去掉没用的行**：范围里不止一行时（可能是几行一起才触发），用 ddmin（逐步删减）去掉没用的行。范围的第一行和最后一行已知有用，固定保留，只在中间找；先只发这两行，两个相距很远的行 1 次请求就确认。
6. **复测**：绕过缓存把结果再发一次，必须再次 429，否则判定为临时限流，不输出结果。
7. **接着找下一处**：在本地把这一处去掉，再发"去掉它的完整提示"。还是 429 就继续找（`--rounds`，默认 3 处），200 就停。结果列出每一处在原提示里的行号和文字。

其他情况：
- 403 / TOS_VIOLATION 最先检查，一出现脚本立刻停下：账号被封了，不是这个问题。
- **结果不确定就停**：502 `MALFORMED_FUNCTION_CALL`（模型生成了坏的工具调用，脚本不带工具，说明请求已经过了谷歌的过滤，既不算通过也不算触发）、503、意外状态码、复测不一致。脚本会清除冷却并停下，**不会**把这类结果当结论缓存。
- 达到 `--max-requests` 上限（默认 25 次）时，脚本会把目前找到的最短触发文字存到 `find_trigger_last.txt`。过一段时间用 `--text-file find_trigger_last.txt` 接着找。
- 默认用日志里的模型测试，要换模型加 `--model 模型名`。
- 同一个 agent 有上一次能通过的系统提示时，加 `--baseline 旧提示文件或旧日志`，新增的行会被优先怀疑，通常 5～6 次请求就够。

**请求数参考**（模拟服务器上实测，不是对谷歌的实测）：

| 情况 | 总请求 | 其中 429 |
| --- | --- | --- |
| 触发句是第一行 | 5 | 3 |
| 给了 `--baseline`，触发句是新增行 | 6 | 3 |
| 触发句在中间、不是嫌疑行 | 约 25（可能撞到默认上限，用 `find_trigger_last.txt` 接着找） | 约 10～11 |
| 复测发现是临时 429 | 5，然后停下 | 3 |

旧脚本（逐行二分加逐词）在第一行触发的真实案例里用了 15 次请求，其中约 11 个是 429。

### 6.3 不用脚本、手动找

原理一样：每次把系统提示的一部分单独当成系统提示发出去，看是 200 还是 429，一半一半地缩小范围。
1. 从日志 `=== REQUEST BODY ===` 下面那行 JSON 里找到系统提示：
   - OpenAI 格式：`messages` 里 `role` 是 `system` 或 `developer` 的 `content`；
   - Gemini 格式：`systemInstruction.parts[].text`；
   - Claude 格式：`system`；
   - Responses 格式：`instructions`。
2. 每次测试发这样一个请求（只改 `system` 的内容）：
   ```bash
   curl -s http://127.0.0.1:8317/v1/chat/completions \
     -H "Authorization: Bearer $CPA_API_KEY" -H 'Content-Type: application/json' \
     -d '{"model":"gemini-3.8-flash-high","max_tokens":256,"messages":[
          {"role":"system","content":"这里放要测的那一段"},
          {"role":"user","content":"Reply with exactly: hello"}]}'
   ```
   系统提示里有引号、换行时，手写 JSON 很容易出错，这就是推荐用脚本的原因。
3. **每次得到 429 后必须清除冷却**，否则下一次请求会直接回 `cooling down`，结果就不准了：
   ```bash
   # 先查每个反重力账号的 auth_index（清除冷却要按账号一个一个清）
   curl -s http://127.0.0.1:8317/v8/management/credentials -H "Authorization: Bearer $CPA_MGMT_KEY" \
     | python3 -c 'import json,sys;[print(f["auth_index"],f["name"]) for f in json.load(sys.stdin)["files"] if f.get("provider")=="antigravity" and not f.get("disabled")]'
   # 再对每个 auth_index 清一次
   curl -s -X POST http://127.0.0.1:8317/v8/management/routing/cooldown/reset \
     -H "Authorization: Bearer $CPA_MGMT_KEY" -H 'Content-Type: application/json' -d '{"auth_index":"这里填上面查到的"}'
   ```
   或者等 1 分钟以上再发。
4. 先测前一半、后一半；哪一半是 429 就继续拆那一半。两半都是 200，说明触发句跨在中间，就从中间往两边扩。一直拆到一两句话、几个词为止。

### 6.4 把找到的触发句写成规则

原则：**改动越小越好，只改谷歌认的那几个字，不要改 agent 干活需要的东西。**

先看触发句属于哪一类，按下表选写法：

| 触发句是什么 | 用哪组 | 怎么写 | 例子 |
| --- | --- | --- | --- |
| 产品名，比较长、不会是别的单词的一部分 | `sensitive-words-replace` | `"产品名": "Antigravity"` | `"AstrBot": "Antigravity"` |
| 产品名很短，或者是普通单词的一部分（比如 Cline 在 decline 里） | `sensitive-patterns-replace` | `'(?i)\b产品名\b'` → `"Antigravity"` | `(?i)\b(Cline\|Aider)\b` |
| 大小写混在一起才安全：大写的是产品名，小写的是路径、技能名、命令 | `sensitive-patterns-replace` | `'\b产品名\b'`，**不加** `(?i)`，只换大写开头的 | `\bHermes\b`、`\bCursor\b` |
| 一整行标题或口号，删掉不影响意思 | `sensitive-patterns-replace` | `'(?m)^标题这一行的写法[ \t]*\r?\n?'` → `""` | `# Google model operational directives` |
| 一句固定说明（规则、约定），不能删 | `sensitive-patterns-replace` | 换个同义说法，意思不变 | `RFC 2119 keywords:` → `Requirement keywords:` |
| 一句普通的英文说明，整句都是触发句 | `sensitive-words-replace` | `"整句原文": "同义改写"` | `"A fact goes in the file for the domain it belongs to, not whichever file happens to be open."` |
| 网址 | `sensitive-words-replace` | `"原网址的域名部分": "别的域名"` | `"hermes-agent.nousresearch.com": "antigravity.nousresearch.com"` |
| 身份句，比如 "You are XX, built by YY." | 优先只换产品名；换了还是 429，再把整句加进 `sensitive-words-replace` 改写 | `"整句原文": "You are a helpful assistant."` | `"Claude Agent SDK": "agent SDK"` |

写规则前必须检查：
1. **会不会误伤**：在完整系统提示里搜这个词，看都出现在哪些地方。如果它也是**工具名、技能名、文件路径、命令**的一部分，就不能用不区分大小写的原文替换，要改用区分大小写的正则，或者只替换触发的那一种写法。Hermes 就是这样：大写的 `Hermes` 和网址会触发，小写的技能名 `hermes-agent`、路径 `D:\...\hermes\` 都不触发；如果全部换掉，技能就加载不到，文件也会写错位置。
2. **替换成什么**：用 `Antigravity`（反重力官方客户端自己的名字），或者删掉。不要换成另一个第三方产品名。
3. **正则怎么写**：
   - 正则必须用**单引号**包起来：`pattern: '...'`；正则里本身有单引号时写成两个 `''`。
   - 这些字符在正则里有特殊含义，要当普通字符用时前面加 `\`：`. ( ) [ ] { } * + ? ^ $ | \`。例如 `Continue.dev` 写成 `Continue\.dev`。
   - `(?i)` = 不区分大小写；`\b` = 单词边界；`(?m)^` = 每行开头。
   - 这是 Go 的 RE2 正则，**不支持** `(?=...)`、`(?<=...)` 这类前后查找。
4. **原文替换**（`sensitive-words-replace`）的左边是普通文字，不用转义；两边都用双引号。

### 6.5 验证

1. 保存配置，确认日志里出现 `antigravity.sensitive-...: X -> Y`（第 4 节）。
2. 用**同一个日志**再跑一次脚本：
   ```bash
   python3 /root/find_trigger.py --log ~/.cli-proxy-api/logs/error-xxxx.log
   ```
   第 2 步显示"未复现"就是成功。还是 429 的话，回到 6.2 继续找；一个系统提示里可能有好几处触发句（Hermes 就有三处），要一处一处找。脚本复测后会提示"去掉它还是 429"，说明还有下一处。
3. 再用真实的 agent 聊一句，确认正常。
4. 在本手册第 10 节的表格里补一行：日期、哪个 agent、触发句、加了什么规则。

### 6.6 收尾

- error 日志里有完整的聊天内容，查完删掉：
  ```bash
  rm -f ~/.cli-proxy-api/logs/error-*.log
  ```
- 删掉服务器上的 `find_trigger_last.txt`（如果有）。
- 清空环境变量：`unset CPA_API_KEY CPA_MGMT_KEY`。

### 6.7 少发 429：降低封号风险

过多的 429 有可能让一个凭证被封（没有证据，也没法排除）。按效果从高到低：

1. **先不发请求，直接比较**：把新系统提示和这个 agent 上次通过的版本做 `diff`（或给脚本 `--baseline`），再对照第 10 节。新增的固定句式（身份句、`powered by`、标题、网址）通常就是触发句，几行而已。
2. **先改后测**：嫌疑行不多时，可以先把它们都写成规则去掉，只发 1 次完整请求验证。200 就解决了，不必知道具体是哪一行；事后再精简规则（手册要求规则越小越好）。
3. **别拿生产账号试错**：有条件就准备一个不重要的反重力账号专门做二分测试。这只能降低损失，不能降低被封概率；如果谷歌按设备或 IP 关联账号，小号被封可能牵连其他号（没有证据，只是风险）。
4. **CPA 会把每个假 429 在上游打两次（2026-11-15 `main.log` 实测）**：一次 429 测试，`conductor_execution` 里对同一个请求编号有两行 `upstream execution failed`，相隔约 11～12 秒，客户端在第二次失败后才收到 429，这就是单次 429 要 11～16 秒的原因。当时配置是 `request-retry: 1`。所以旧脚本那次 15 个请求（约 11 个 429）实际向上游打了约 20 次 429。已被 CPA 冷却直接拒绝的请求（耗时几毫秒）没有上游记录。
   - 把 `request-retry` 临时设为 0 大概能把每个 429 的上游次数减半、耗时缩到 2～4 秒。**没有试过。**配置注释写的是"403、408、5xx 才重试"，和日志里 429 也被重试不一致，原因我没查清，所以别假设改了就一定有效。
   - 这是全局设置，会影响所有真实用户的重试：只在测试期间改，测完**必须改回**，改之前先问用户。
5. **节奏**：`--max-requests` 默认 25，`--pause` 默认 3 秒，别调低。
6. **只有一个账号在用，被封就停摆**：考虑开 `tos-pause-window`（第 8 节），一个号被封后暂停整个池子，不让剩下的号继续去撞。

### 6.8 三方审查记录（2026-11-15，针对 find_trigger.py）

让三个模型独立审查了脚本，Astra 的结论对着代码核过，以它为准：

| 问题 | 结论 |
| --- | --- |
| 任何 429 都缓存为"触发"，一次无关的 429 会让 `largest_start` 得到错误窗口 | 已确认；复测必须绕过缓存。`largest_start` 返回 0 本身是合法的，错的是缓存 |
| 逐词得到的短语没单独验证（正则丢末尾空白，输出又 `.strip()`） | 已确认；默认不做逐词，复测用实际输出的文字 |
| 503、意外状态码在 `Abort` 前没清冷却 | 已确认；但不是所有错误都该盲目清冷却，只在"结果不确定"时清并停下 |
| 502 `MALFORMED_FUNCTION_CALL` 能算"通过" | **不采纳**：它既不能证明通过也不能证明触发，改为"结果不确定，停下" |
| 去掉候选后 200 就证明只有这一个触发句 | **不采纳**：可能删掉了多处，也可能改了上下文，只当线索 |
| 完整提示 200 = "现有规则已覆盖" | **不采纳**：只是"未复现"，文案已改 |
| 倍增搜索 | 部分采纳：触发句靠后时反而更贵，只试前 1、2、4、8 行，之后回到二分 |
| k 路搜索、请求前先整份重放 | 不采纳：顺序执行不比二分少；重放放在"完整提示 200"之后才有意义 |
| 降低 `request-retry` | 暂不改：日志已确认每个 429 会在上游打两次（6.7 第 4 条），但改它影响真实用户，要用户同意，且效果没验证 |

### 6.9 和 cpa-sentinel 对比后整合的部分（2026-11-16）

朋友的 [cpa-sentinel](https://github.com/CicoriOS/cpa-sentinel)（Rust，常驻监听日志、自动找触发行、自动写规则）。采纳和没采纳的：

| 它的做法 | 处理 |
| --- | --- |
| ddmin：能找出"几行一起才触发" | **采纳**，见 6.2 第 5 步；固定范围首尾两行，比它直接对整份提示做 ddmin 便宜（模拟：相距 60 行的两行，不固定首尾要 60 次以上，固定后 23 次，其中包含对照、完整提示、复测） |
| 一处找完在本地去掉，接着找下一处 | **采纳**，见 6.2 第 7 步（`--rounds`） |
| 先发 `hi` 判断是不是真限流 | 已有（第 1 步对照组） |
| 让另一个模型把触发行改写成别的语言或更长的句子 | **后来采纳（cpa_guard）**：改成"翻译成中文"，由 CPA 里**非反重力**渠道的模型翻译，路径、命令、工具名原样保留，翻译后还要经 CPA 验证不再 429；不删行 |
| 自动监听日志、自动改配置、没人确认 | **后来采纳（cpa_guard）**，但加了它没有的保护：429 预算、遇 403 立刻停、改前备份、写后读回核对、没验证的规则自动撤回（第 11 节） |
| 整行字面量规则 | **不采纳**：触发行带日期、路径时下次就匹配不上，我们用正则 |
| 只认 Gemini 格式 | 我们三种格式都读 |

CPA 8.0.73～8.0.76 和这件事相关的变化（只看了更新说明）：`payload.replace`（8.0.75，只改用户消息，不改系统提示，不能替代 `sensitive-patterns-replace`）；Gemini 无输出的 502 不再进冷却（8.0.73，可能解决 502 后 503 的问题，没验证）；示例配置里 `request-retry` 默认 3 且 429 也会重试（我们用 1）。

仍然没解决的风险：CPA 的正则如果带 `^` 锚点，对切片后的文字单调性可能被破坏；脚本提取系统提示时用换行拼接并换了请求格式，所以和真实请求不完全等价。

---

## 7. 升级

- **CPA 升级**：常用的安装脚本（brokechubb 的 cliproxyapi-installer）升级时会先备份 `config.yaml`，装完再放回去，规则不会丢。升级后检查一下：
  ```bash
  grep -nA3 -E 'sensitive-(patterns|words)-replace:' config.yaml
  ```
  能看到规则就行。CPA 以后如果改了这两个配置项的名字，要照新版 `config.example.yaml` 改过去。
- **agent 升级**：规则在 CPA 上，agent 升级不会让规则消失。但新版 agent 可能改了系统提示的措辞，出现新的触发句。升级后聊一句检查（第 5 节）。
- **CPA 放在 nginx 后面时，管理页可能"输对密码也被封"**（2026-11-19，8.0.78 上遇到）：管理接口同一个 IP 输错 5 次（不带密钥也算）就封 30 分钟。新版的 `server.trusted-proxies` 默认是空列表，意思是不信任 nginx 带的 `X-Forwarded-For`，所有经 nginx 的请求都算 `127.0.0.1`。网上有扫描器（UA `cpa-batch/1.0`）不停猜管理密钥，它们猜错 5 次，就把 `127.0.0.1` 封了，你也一起被封（提示 `IP banned due to too many failed attempts`）。修法：在 `config.yaml` 的 `server` 下加
  ```yaml
  trusted-proxies:
    - 127.0.0.1
  ```
  然后**重启 CPA**（这一项不会热重载；重启也会清掉现有的封禁）。之后按访客真实 IP 计数。前提：CPA 只监听 `127.0.0.1`，外面只能经 nginx 进来，否则别人可以伪造 `X-Forwarded-For`。

---

## 8. 封号（403 TOS_VIOLATION）和账号安全

- 这和假 429 是**两回事**。规则只能解决 429，防不了封号。2026-11 有过几波集中封号（例如 11-06、11-14），只用官方客户端的人也有被封的。
- 被封的号：重新登录也没用，第一个请求就会 403。只能申诉：https://forms.gle/hGzM9MEUv2azZsrb9 。
- CPA 8.0.70 起，号被封后会自动停用。还有一个可选设置 `oauth.providers.antigravity.tos-pause-window`（例如 `"6h"`）：池子里第一个号被封后，整个池子暂停这么久，不让剩下的号继续去撞。要不要开，自己决定。
- 测试时少发请求。故意触发的 429 会不会提高被封的概率，没有证据，但也没法排除。

---

## 9. 给 AI 维护者的硬性规则

1. **改 `config.yaml` 前先备份**：`cp config.yaml config.yaml.bak-$(date +%Y%m%d-%H%M)`。
2. **只往两组规则里添加条目**，不要删除或改写已有的条目，除非用户明确要求。删掉以后原来的 429 会回来。
3. **不要用 `sensitive-words`（零宽字符混淆）**，要用替换或删除。
4. **不要重新启用 `antigravity-prompt-filter` 插件**，规则已经包含它的词表。
5. **不要替换常见英文单词**，例如 `assistant`、`agent`、`code`、`cursor`（小写）、`claude`（单独一个词）。这样会改坏大量句子，影响回答质量。只替换产品名或谷歌认的那句原文。
6. **不要碰工具名、技能名、文件路径、命令**。如果触发句正好是这些东西的一部分，用区分大小写的正则只换触发的写法（见第 6.4 节）。
7. **测试请求要少**：每次 429 后都要清除冷却或等冷却结束；对照组都 429 了就停。看到 403 TOS_VIOLATION 立刻停，告诉用户。有上一版提示时先用 `--baseline` 或 diff 缩小范围（第 6.7 节）。脚本报"结果不确定"（502、503、复测不一致）时不要硬重试，先查原因；不要擅自改 `request-retry` 之类影响真实用户的全局设置。
8. **不要把任何密钥打印或贴到聊天里**。读 `config.yaml` 时把密钥打码，把密钥放进环境变量传给脚本。
9. 规则改完，必须**用同一个日志重跑脚本**（第 6.5 节），看到"未复现"才算完成；再请用户用真实 agent 试一句。
10. 每次新加规则，都在第 10 节表格里记一行。
11. 密钥一旦在输出里露出来（例如读密钥文件时没打码），要马上告诉用户，建议换掉。

### 9.1 AI 操作清单（照做，不要自己发挥）

用户说"某个 agent 报 429"时，**按顺序**做，每一步的输出都要看，不要跳步。

**第 0 步：先问清楚**（没有这些信息就问用户，不要猜）：报错原文、模型名、哪个 agent、什么时候开始、有没有升级过 agent。对照第 1.1 节判断是不是假 429。403 / TOS_VIOLATION 不是本手册要解决的问题，告诉用户去申诉，停。

**第 1 步：看日志，不发请求**
1. 连上 CPA 服务器。
2. `ls -t ~/.cli-proxy-api/logs/error-*.log | head`，找最新、模型名对得上的 error 日志。注意：耗时几毫秒、响应里有 `cooling down` 的那条是 CPA 自己拒绝的，**不是**要找的日志；要找响应里有 `upstream execution failed` 的那条。
3. 看 `main.log`：同一个请求编号下 `upstream execution failed` 出现几次、`request-retry` 现在是几（`grep -n request-retry config.yaml`）。
4. 确认 `config.yaml` 里 `logs-max-total-size-mb: 50`、`error-logs-max-files: 300`，不是就先告诉用户再改。
5. 如果有这个 agent 上一次能通过的提示或日志，记下来，下一步用 `--baseline`。

**第 2 步：上传并预览脚本**（脚本在这个文件夹里，服务器上默认没有；用完要删）
```bash
python3 /root/find_trigger.py --log <日志路径> --dry-run
```
只会显示提取到的系统提示（字符数、行数、开头 300 字符），**不发请求**。系统提示为空，说明这个 429 和系统提示无关，停，别继续测。

**第 3 步：正式找**（密钥只用环境变量传，**不要打印、不要贴到聊天里**）
- API 密钥：`config.yaml` 里 `access:` → `api-keys:` 下任意一个。
- 管理密钥：登录 CPA 网页面板用的那个（用户自己保存的原文）。读存放它的文件时**必须打码**，只取需要的那一行放进环境变量。
```bash
python3 /root/find_trigger.py --log <日志路径> [--baseline <上一版>]
```

**第 4 步：看脚本结果，按下表办**

| 脚本输出 | 意思 | 你该做什么 |
| --- | --- | --- |
| 对照组也是 429 | 真没额度或被限流 | 停，告诉用户，不要继续测 |
| 第 2 步"未复现" | 完整提示这次没触发 | 不要加规则。先把日志里的原始请求重放一次；还是 200 就是临时限流，告诉用户这是临时问题 |
| "找到了：……（已复测一次）" | 找到触发句 | 去第 5 步 |
| "复测没有再触发 429" | 之前是临时 429 | 不要加规则，过几分钟重跑 |
| "HTTP 502 MALFORMED_FUNCTION_CALL" / 503 / "意外的回应" | 结果不确定 | 不要硬重试，不要当成通过，告诉用户 |
| 403 / TOS_VIOLATION | 账号被封 | 立刻停，告诉用户 |
| 达到 `--max-requests` 上限 | 没找完 | 告诉用户，别自己调大上限；用 `find_trigger_last.txt` 过一阵再接着找 |
| 打印了"去掉它以后还是 429" | 还有别的触发句 | 先加这一条规则，再用同一个日志重跑找下一处 |

**第 5 步：加规则**
1. 备份：`cp config.yaml config.yaml.bak-$(date +%Y%m%d-%H%M)`。
2. 按第 6.4 节的表选写法：只往 `sensitive-words-replace` 或 `sensitive-patterns-replace` 里**追加**，不要改已有条目，不要换常见单词，不要碰路径、工具名。
3. 先看这个词会不会在别处误伤（6.4 的"写规则前必须检查"）。
4. 保存后确认 `main.log` 里出现 `antigravity.sensitive-...: X -> Y`，没有重启。

**第 6 步：验证**：用同一个日志重跑脚本，第 2 步显示"未复现"才算成功；再请用户用真实 agent 发一句话，两个模型都试。

**第 7 步：收尾**：在第 10 节补一行；删掉服务器上的 `error-*.log`、`find_trigger.py`、`find_trigger_last.txt`；`unset CPA_API_KEY CPA_MGMT_KEY`；告诉用户 `config.yaml.bak-*` 还在，由他决定留不留。

**什么时候必须问用户，不能自己决定**：改 `request-retry` 等全局设置；调大 `--max-requests`；用停用的账号做测试；删除已有规则；任何会重启 CPA 的操作；对账号可能有风险的事。

---

## 10. 已知触发句清单（加新规则时在这里补一行）

| 日期 | 来源 | 触发句（单独作为系统提示就会 429） | 处理规则 |
| --- | --- | --- | --- |
| 2026-07-31 | Hermes（社区） | `You are Hermes Agent, an intelligent AI assistant created by Nous Research.` | 产品名替换（`Hermes`、`Nous Research`） |
| 2026-09-11 | omp 旧版（社区） | `<system-conventions>` + `RFC 2119: MUST, REQUIRED, SHOULD, RECOMMENDED, MAY, OPTIONAL.` | `system-conventions` → `conventions`；`RFC 2119:` 改写 |
| 2026-09-15 | Codex（社区） | `You are Codex, an agent based on GPT-5.` | `Codex` → `Antigravity` |
| 2026-10-27 | Claude Code 子代理（社区） | `You are a Claude agent, built on Anthropic's Claude Agent SDK.` | `Claude Agent SDK` → `agent SDK` |
| 2026-11-10 | omp 新版（社区） | `RFC 2119 keywords: MUST, REQUIRED, SHOULD, RECOMMENDED, MAY, OPTIONAL.` | 正则改写成 `Requirement keywords:` |
| 2026-11-15 | Hermes（实测） | `# Google model operational directives`（Hermes 用 Gemini 时自动加的标题，不在 SOUL.md 里） | 正则删除这一行 |
| 2026-11-15 | Hermes（实测） | 系统提示里大写开头的 `Hermes`（例如 "Hermes delivers their message..."） | 区分大小写正则 `\bHermes\b` |
| 2026-11-15 | Hermes（实测） | 文档网址 `https://hermes-agent.nousresearch.com/docs` | 网址替换 |
| 2026-11-15 | AstrBot（实测） | `AstrBot`（系统提示里的技能路径、说明文字都有） | `AstrBot` → `Antigravity` |
| 2026-11-15 | 安卓聊天 App 的记忆功能（实测，`<memory_format>` 段落里） | `A fact goes in the file for the domain it belongs to, not whichever file happens to be open.` | 整句同义改写 |
| 2026-11-15 | WorkBuddy（实测，gemini-3.8-flash-high、gemini-4-hydrogen-xhigh 都中） | 系统提示第一行 `This conversation is powered by gemini-3.8-flash-high`（拆成单词都不触发，整行才触发） | 正则整行删除 `(?m)^This conversation is powered by [^\r\n]*\r?\n?`，不绑定模型名 |
| 2026-11-17 | Hermes（cpa_guard 实测，gemini-3.8-flash-high） | 两行各自单独就会触发：`When the user asks you to build, run, or verify something, the deliverable is a working artifact backed by real tool output — not a description of one. …` 和 `If a tool, install, or network call fails and blocks the real path, say so directly and try an alternative … NEVER substitute plausible-looking fabricated output …` | cpa_guard 加了两条整行正则，把这两行换成 `gpt-6-luna` 翻译的中文版；触发文字和完整提示经 CPA 都返回 200 |
| 2026-11-19 | WorkBuddy 新版（cpa_guard 实测，gemini-4-neon-xhigh；CPA 8.0.79） | 第 10 行安全声明 `IMPORTANT: Assist with authorized security testing, defensive security, CTF challenges, and educational contexts. Refuse requests for destructive techniques, DoS attacks, …`，翻译成中文仍 429（按意思拦）。同一提示里新的身份句 `You are powered by the model named gemini-4-neon-xhigh…` 单独替换后仍 429，不是这次的原因 | 人工决定整行删除 `(?m)^[ \t]*IMPORTANT:[ \t]+Assist[ \t]+with[ \t]+authorized[ \t]+security[ \t]+testing[^\r\n]*\r?\n?`，完整提示经 CPA 返回 200 |
| 2026-11-19 | Hermes 新版开场白（`SOUL.md` 第 1 行，cpa_guard + 逐句实测，gemini-3.8-flash-high） | 第 1 行里只有一句会触发：`Be direct: match the length of your reply to the weight of the ask — a one-line question gets a one-line answer, and finished work gets a short report of what changed, what's verified, and what's left, never a replay of the process.`（同一行的身份句和其余 4 句都 200）。cpa_guard 把整行（664 字）翻成中文 3 次都仍 429 | 人工加了只换这一句的正则 `Be[ \t]+direct:…never[ \t]+a[ \t]+replay[ \t]+of[ \t]+the[ \t]+process\.` → `回答要简洁，篇幅和问题的分量相当：简单的问题简短回答；任务做完后简要说明改了什么、确认了什么、还剩什么，不复述过程。`，原始第 1 行经 CPA 返回 200 |
| 2026-11-24 | 用 Claude Agent SDK（TypeScript）的客户端直连 CPA（cpa_guard 实测，gemini-3.8-flash-high；CPA 8.1.0） | 两行各自单独就会触发：第 1 行计费标记 `x-anthropic-billing-header: cc_version=…; cc_entrypoint=sdk-ts;` 和第 4 行 `You are an interactive agent that helps users with software engineering tasks.` | 计费标记全是代码、没法翻译，人工同意后整行删除 `(?m)^[ \t]*x-anthropic-billing-header:[^\r\n]*\r?\n?`（不绑定版本号），经 CPA 返回 200；第 4 行交给 cpa_guard 翻译成中文。当时翻译模型 `gpt-6-luna` 过载（502），cpa_guard 改为暂存、15 分钟后重试，不再直接放弃 |

"社区"条目的出处：CLIProxyAPI issue #4696、#5751、#5848、#7049，oh-my-pi issue #18706。

2026-11-15 实测**不会**触发的（不要为它们加规则）：小写技能名 `hermes-agent`、`D:\...\hermes\...` 路径、工具定义、聊天记录里的 hermes、Hindsight 记忆整理的系统提示。

---

## 11. 自动处理：cpa_guard（在 CPA 服务器上常驻运行）

不想手动找触发句时用它。它盯着 CPA 的 `error-*.log`，发现新的反重力假 429 就自己找触发行、写规则、验证，**全程不需要人决定**。人只会收到结果：`state/events.log`，配了 `BARK_URL` 就推送到手机（Bark），也可以用 `--notify-cmd` 接别的通知。

### 它怎么做（对着 `cpa_guard.py`）

1. **只处理真的假 429**：日志里是上游的 `RESOURCE_EXHAUSTED`、不是 CPA 自己的"cooling down"、模型是反重力的（`--model-regex`），而且不是它自己发的测试请求。
2. **429 预算**：每个 429 探测算 `1 + request-retry` 次上游命中（实测一个假 429 在上游被打两次）。默认每小时最多 10 次命中、每天最多 24 次，用完就暂停，已缩小的范围存下来，之后接着查。
3. **先用不发 429 的办法**：对比同一个 agent 上一次通过的提示，新增行最可疑；再看第一行、`powered by`、`You are`、网址、标题。一个能直接命中的触发句，约 2 个 429（4 次上游命中）。
4. **写规则走 CPA 的管理接口**：GET 读出 `antigravity` 下的整个配置，PATCH 写回"旧规则 + 新规则"，再读回核对：只允许规则列表变化、而且只多了这一条。读不到现有规则就不写（以后再试）；读回和预期不一致（例如 PATCH 变成了追加，或词替换没了），就**不再写任何东西、停止自动改规则**，发 MANUAL 事件写明哪里不对和备份位置，要 `--resume` 才恢复。写进去但还没验证的规则（规则和替换文字）记在状态里；有这种规则时 guard 先把它撤回，撤回之前不调查、不写新规则；程序中途崩了，下次启动也会先撤回。撤回时重新读一次当前列表，只删规则和替换文字都对得上的那一条，人在面板里加的或改过的规则不受影响。2026-11-16 在真实服务器上做过一次"原样写回"的测试：规则、词替换、`antigravity-credits` 都不变；CPA 会重写 `config.yaml`，注释的缩进会变、少了几条注释，规则内容等价。
5. **规则怎么写**：
   - **不删行，改成中文**：请一个**非反重力**的模型（`--rewrite-model`，默认 `gpt-6-luna`，走 CPA 的 Codex 渠道，碰不到谷歌账号，被引用的触发句也不会让它 429）把触发行翻译成简体中文；文件路径、命令、工具名（从出错请求里的工具清单读出来）、网址、代码原样保留，不合格的翻译不用。规则是整行锚定的正则（`^…$`，数字写成 `[0-9]+`），把这一行换成中文版。
   - **长行先逐句定位**：触发行里有 2～12 句时，先把每一句单独发一次（200 不花预算，每个 429 花一次），只翻译单独就会 429 的那几句，同一行的其他句子不动（规则只换那一句，不锚定整行）。没有哪句单独触发（是几句合起来才触发）时，照旧整行翻译。起因：2026-11-19 Hermes 新开场白 664 字的一行里只有一句触发，整行翻译 3 次都还 429。
   - 每个中文版都要经 CPA 验证不再 429；最多试 3 个。如果直接翻译后仍然 429（说明谷歌是按意思拦的，比如提到 DoS、C2、漏洞利用的安全声明），后面几次改成"意思相近、但不点名具体攻击手段、产品或敏感话题"的中文。都不行就**不加规则**，只通知。设计上**从不删行**：删掉一行说明（尤其是工具用法）可能让 agent 悄悄变笨，比 429 更难发现；确实要删的（例如 2026-11-19 WorkBuddy 的安全声明）由人决定后手动加规则。
   - 已有的规则会先改写这一行时，新规则按改写后的样子生成（新规则排在后面执行）。
6. **每条规则写完立刻验证**：把触发文字原样发给 CPA，必须 200（没有 429 才算有效）；不行就撤回，试下一个候选。全部规则写完再发一次完整提示。中途被打断（预算用完、403 等）时，已经单独验证有效的规则保留，正在验证的那条撤回。
7. **遇到下面这些就停，不乱试**：
   - 探测或真实流量里出现 403 / TOS_VIOLATION（调查进行中也会每次发请求前检查）：所有测试暂停，要在服务器上执行 `python3 cpa_guard.py --resume` 才会恢复（`STOP` 文件也能暂停）。
   - 对照组也是 429（账号真没额度）：30 分钟内不测。
   - 结果不确定（502、503、复测不再 429）：不加规则，也不缓存。
   - 同一个 agent 24 小时内已调查 3 次：暂停调查它 24 小时（发一次 PAUSE 事件）；24 小时内已加 3 条规则：不再自动加。
   - 暂存的调查最多接着查 3 次；查不完时，已经确认的触发句照样加规则。

### 部署（以 root、brokechubb 安装脚本的默认路径为例）

1. 把 `cpa_guard.py`、`find_trigger.py` 放到 `/root/cpa-guard/`。
2. 建一个只有 root 能读的环境文件 `/root/cpa-guard/guard.env`（`chmod 600`）：
   ```bash
   CPA_MGMT_KEY=你的管理密钥
   BARK_URL=https://api.day.app/你的Bark密钥     # 不用 Bark 就不写这行
   # CPA_API_KEY 可以不写，脚本会从 config.yaml 的 api-keys 读第一个
   ```
3. 先发一条测试通知：`set -a; . /root/cpa-guard/guard.env; set +a; python3 /root/cpa-guard/cpa_guard.py --test-notify`。
4. 建 `~/.config/systemd/user/cpa-guard.service`：
   ```ini
   [Unit]
   Description=cpa_guard
   After=cliproxyapi.service

   [Service]
   EnvironmentFile=/root/cpa-guard/guard.env
   ExecStart=/usr/bin/python3 -u /root/cpa-guard/cpa_guard.py --no-apply
   Restart=always
   RestartSec=30

   [Install]
   WantedBy=default.target
   ```
   然后 `systemctl --user daemon-reload && systemctl --user enable --now cpa-guard`（root 的用户级服务可能要先 `export XDG_RUNTIME_DIR=/run/user/0`）。
5. 建议先用 `--no-apply`（只找、只报告，不改规则）跑一两次，看推送里找到的句子和打算的处理对不对，再去掉 `--no-apply` 重启服务。
6. 路径不一样就加参数：`--cpa-dir`（`config.yaml` 所在目录）、`--log-dir`、`--state-dir`、`--base`（CPA 地址）。翻译模型 `--rewrite-model` 必须是你 CPA 里**非反重力**渠道的模型。

运行后的文件：`state/state.json`（预算、暂存的调查等）、`state/events.log`（所有结果）、`state/backups/`（每次改规则前的 `config.yaml` 和 `antigravity` 配置 JSON，各保留最近 10 个，权限 600）。

常用命令：`python3 cpa_guard.py --status`（看预算、是否停止、自动改规则是否被停、有没有暂存的调查和没验证的规则）、`--resume`（清除 403 停止和"停止自动改规则"两个标志）、`--once 某个日志 --dry-run`（只分析不发请求，不改状态）、`--no-apply`（只报告不改规则）、`--hourly-budget` / `--daily-budget`（429 上限，默认 10 / 24 次上游命中）。

### 自己跑测试（不碰真实账号）

`tests/` 里有一个假 CPA（`fake_cpa.py`，模拟 429、403、各种管理接口异常和翻译模型），在本机运行即可：

```bash
cd tests
./run.sh t1 '["This conversation is powered by"]' normal replace fixtures/powered_by.txt   # 普通触发句：应 FIXED
./run.sh t2 '["x"]' tos replace fixtures/powered_by.txt                                    # 403：应 HALT
./run.sh t3 '["This conversation is powered by"]' normal append fixtures/powered_by.txt    # PATCH 变追加：应停止自动改规则
python3 resume_test.py r1 '["Alpha special trigger","Bravo special trigger"]' fixtures/two_triggers.txt 8   # 预算不够时暂存再恢复
python3 recover_test.py                                                                     # 崩溃后撤回没验证的规则
```

`run.sh` 的参数：名字、触发词 JSON、`normal|tos`、管理接口行为（`replace` 正常 / `ignore` 不生效 / `append` 追加 / `dropwords` 删词替换 / `timeout` 报错但已写入 / `getfail` 读失败）、系统提示文件，后面可以再接 `cpa_guard.py` 的参数。环境变量 `REWRITER=echo` 让假翻译模型原样返回（模拟翻译失败）。

### 测试情况（只写做过的）

- 假 CPA：上面这些场景，以及路径行/工具行翻译成功、翻译失败不加规则、两处触发中途暂存、`--dry-run` 守护进程收到 403 日志。
- 真实 CPA（8.0.76）：管理接口读规则、原样写回、`/v8/management/credentials`、清冷却接口都测过。
- 真实使用（2026-11-17，Hermes + gemini-3.8-flash-high）：自动找到两处触发句（第 10 节最后一行），换成中文后触发文字和完整提示都返回 200。找全用了约 19 个 429（约 38 次上游命中），所以默认预算会让它分几个小时完成。
- 三轮代码审查的结论和修改见 `REVIEW.md`。
