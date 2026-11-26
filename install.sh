#!/usr/bin/env bash
# cpa_guard 一键安装 / 升级（在 CPA 所在的服务器上，用 root 运行）。
#   bash install.sh           检查 → 发测试通知 → 安装并启动 systemd 服务（已装过就是升级、重启）
#   bash install.sh --check   只检查、发测试通知，不安装
# 第一次运行会生成 /root/cpa-guard/guard.env 然后停下，填好再运行一次。
set -euo pipefail

SRC=$(cd "$(dirname "$0")" && pwd)
INSTALL_DIR=${INSTALL_DIR:-/root/cpa-guard}
export INSTALL_DIR
SERVICE=${SERVICE_NAME:-cpa-guard}
ENVF=$INSTALL_DIR/guard.env
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

ok() { echo "  [OK] $*"; }
fail() {
    {
        echo
        echo "[失败] $*"
        echo "安装没有完成。请把上面的失败原因原样告诉主人；不要改 cpa_guard.py 或 CPA 的文件来绕过。"
    } >&2
    exit 1
}

echo "== 1/6 检查运行环境"
[ "$(id -u)" = 0 ] || fail "要用 root 运行：sudo bash install.sh"
PY=$(command -v python3) || fail "没有 python3。Debian/Ubuntu 先执行：apt-get install -y python3"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 8))' || fail "python3 版本太旧，要 3.8 或更新"
command -v systemctl >/dev/null || fail "这台机器没有 systemd（systemctl），本脚本不支持"
[ -f "$SRC/cpa_guard.py" ] && [ -f "$SRC/find_trigger.py" ] && [ -f "$SRC/guard.env.example" ] \
    || fail "install.sh 旁边缺少 cpa_guard.py / find_trigger.py / guard.env.example，请下载完整的仓库"
if [ "$CHECK_ONLY" = 0 ] && [ "$SERVICE" = cpa-guard ] && [ -f /root/.config/systemd/user/cpa-guard.service ]; then
    fail "这台机器已经用旧办法（root 的用户级服务）装过 cpa_guard，再装会有两个同时运行。先让主人决定删掉哪一个"
fi
ok "root、$("$PY" -V)、systemd"

echo "== 2/6 读取配置 $ENVF"
mkdir -p "$INSTALL_DIR"
chmod 700 "$INSTALL_DIR"
if [ ! -f "$ENVF" ]; then
    (umask 077; cp "$SRC/guard.env.example" "$ENVF")
    echo
    echo "已生成配置文件：$ENVF"
    echo "下一步：编辑这个文件，至少填 CPA_MGMT_KEY 和邮件（SMTP_HOST、SMTP_USER、SMTP_PASS），"
    echo "然后再运行一次：bash $SRC/install.sh"
    exit 2
fi
chmod 600 "$ENVF"
set -a
# shellcheck disable=SC1090
. "$ENVF" || fail "$ENVF 格式不对（每行 KEY='值'，说明只能写在 # 开头的单独一行）"
set +a
[ -n "${CPA_MGMT_KEY:-}" ] || fail "$ENVF 里 CPA_MGMT_KEY 没填"
[ -n "${SMTP_HOST:-}${BARK_URL:-}" ] || fail "$ENVF 里邮件（SMTP_HOST 等）和 BARK_URL 至少要填一种，否则出事了没人知道"
if [ -n "${SMTP_HOST:-}" ]; then
    [ -n "${SMTP_USER:-}" ] || fail "$ENVF 里填了 SMTP_HOST，但 SMTP_USER（发件邮箱）没填"
    [ -n "${SMTP_PASS:-}" ] || fail "$ENVF 里填了 SMTP_HOST，但 SMTP_PASS（授权码）没填"
    case "${SMTP_PORT:-465}" in 465|587) ;; *) fail "SMTP_PORT 只能是 465 或 587，现在是 ${SMTP_PORT}" ;; esac
fi
ok "guard.env 已填写"

echo "== 3/6 找 CPA、检查管理密钥和模型"
DETECTED=$(cd "$SRC" && "$PY" - <<'PY'
import glob, json, os, re, shlex, sys, urllib.error, urllib.request
import cpa_guard

def say(msg):
    print("  " + msg, file=sys.stderr)

def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)

def env(name):
    return os.environ.get(name, "").strip()

# --- config.yaml
if env("CPA_DIR"):
    dirs = [env("CPA_DIR")]
    if not os.path.isfile(os.path.join(dirs[0], "config.yaml")):
        die(f"CPA_DIR={dirs[0]} 里没有 config.yaml")
else:
    found = set()
    for top in ("/root", "/opt", "/home", "/srv", "/usr/local", "/etc"):
        for depth in ("*", "*/*", "*/*/*"):
            for p in glob.glob(os.path.join(top, depth, "config.yaml")):
                if "node_modules" in p or p.startswith(os.environ["INSTALL_DIR"] + "/"):
                    continue
                try:
                    t = open(p, encoding="utf-8", errors="replace").read()
                except OSError:
                    continue
                if re.search(r"^\s*auth-dir\s*:", t, re.M) and "api-keys" in t:
                    found.add(os.path.dirname(os.path.realpath(p)))
    dirs = sorted(found)
    if not dirs:
        die("找不到 CPA 的 config.yaml。请在 guard.env 里填 CPA_DIR='config.yaml 所在的目录'")
    if len(dirs) > 1:
        die("找到好几个像 CPA 的 config.yaml：" + "、".join(dirs) + "。请在 guard.env 里填 CPA_DIR 选定一个")
cpa_dir = dirs[0]
cfg = open(os.path.join(cpa_dir, "config.yaml"), encoding="utf-8", errors="replace").read()
say(f"[OK] CPA 配置：{cpa_dir}/config.yaml")

# --- address
m = re.search(r"^port\s*:\s*[\"']?(\d+)", cfg, re.M)
base = (env("CPA_BASE") or f"http://127.0.0.1:{m.group(1) if m else 8317}").rstrip("/")

def call(path, key):
    req = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except OSError as e:
        die(f"连不上 CPA（{base}）：{e.__class__.__name__}。CPA 没在运行，或者地址不对（可在 guard.env 填 CPA_BASE）")

# --- management key
code, _ = call(cpa_guard.ft.MGMT + "/config", env("CPA_MGMT_KEY"))
if code in (401, 403):
    die("CPA_MGMT_KEY 不对：CPA 拒绝了（要填管理面板的登录密码明文）")
if code == 404:
    die("CPA 没有管理接口：config.yaml 里 remote-management 的 secret-key 没设，或者 CPA 版本太旧（要 8.1 以上）")
if code != 200:
    die(f"CPA 管理接口返回 {code}，不正常")
code, body = call(cpa_guard.ft.MGMT + cpa_guard.PROVIDER, env("CPA_MGMT_KEY"))
if code == 404:
    die("CPA 的 config.yaml 里没有 oauth → providers → antigravity 这一段。"
        "照仓库 README 第 3 节把那段配置加进 config.yaml，重启 CPA，再运行本脚本")
if code != 200:
    die(f"读取反重力配置返回 {code}，不正常")
say(f"[OK] 管理密钥正确，CPA 地址 {base}")
if "sensitive-patterns-replace" not in json.loads(body or b"{}"):
    say("[提醒] 反重力配置里还没有基础规则。不加也能用，但建议照 README 第 3 节加上，能少触发很多 429")

# --- client key + models
api_key = env("CPA_API_KEY") or cpa_guard.client_key(cfg)
if not api_key:
    die("config.yaml 里找不到 api-keys，请在 guard.env 填 CPA_API_KEY")
code, body = call("/v1/models", api_key)
if code != 200:
    die(f"用客户端密钥列模型返回 {code}。请在 guard.env 填一个能用的 CPA_API_KEY")
models = [x.get("id", "") for x in json.loads(body).get("data", [])]
wanted = [x.strip() for x in env("REWRITE_MODEL").split(",") if x.strip()] or ["deepseek-flash", "gemini-3.8-flash-high"]
missing = [x for x in wanted if x not in models]
rewrite = [x for x in wanted if x in models]
if env("REWRITE_MODEL") and missing:
    die("REWRITE_MODEL 里这些模型 CPA 没有：" + "、".join(missing))
if not rewrite:
    flash = [x for x in models if "flash" in x]
    die("CPA 里没有默认的翻译模型（deepseek-flash、gemini-3.8-flash-high）。请在 guard.env 填 REWRITE_MODEL，"
        "选一个便宜、快的模型，例如：" + "、".join(flash[:8] or models[:8]))
say("[OK] 翻译模型：" + ",".join(rewrite))

# --- logs
cand = [env("LOG_DIR")] if env("LOG_DIR") else []
if not cand:
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if "cli-proxy" not in os.readlink(f"/proc/{pid}/exe").lower():
                continue
            for fd in os.listdir(f"/proc/{pid}/fd"):
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
                if target.endswith(".log"):
                    cand.append(os.path.dirname(target))
        except OSError:
            continue
    m = re.search(r"^\s*auth-dir\s*:\s*[\"']?([^\"'\s#]+)", cfg, re.M)
    if m:
        cand.append(os.path.join(os.path.expanduser(m.group(1)), "logs"))
    cand.append(os.path.join(cpa_dir, "logs"))
log_dir = next((d for d in cand if os.path.isdir(d)), "")
if not log_dir:
    die("找不到 CPA 的日志目录（试过：" + "、".join(cand) + "）。请在 guard.env 填 LOG_DIR")
say(f"[OK] 日志目录：{log_dir}")
if not re.search(r"^\s*error-logs-max-files\s*:\s*[1-9]", cfg, re.M):
    say("[提醒] config.yaml 里没设 error-logs-max-files（建议 300，见 README 第 3 节），出错日志可能留不住")

for k, v in (("CPA_DIR", cpa_dir), ("LOG_DIR", log_dir), ("CPA_BASE", base), ("REWRITE", ",".join(rewrite))):
    print(f"{k}={shlex.quote(v)}")
PY
) || fail "原因见上一行"
eval "$DETECTED"

echo "== 4/6 复制程序到 $INSTALL_DIR"
if [ "$SRC" != "$INSTALL_DIR" ]; then
    install -m 700 "$SRC/cpa_guard.py" "$SRC/find_trigger.py" "$INSTALL_DIR/"
fi
ok "cpa_guard.py、find_trigger.py"

echo "== 5/6 发测试通知"
"$PY" "$INSTALL_DIR/cpa_guard.py" --cpa-dir "$CPA_DIR" --state-dir "$INSTALL_DIR/state" --test-notify \
    || fail "测试通知没发出去，原因见上面\"通知失败\"那行。常见原因：SMTP_PASS 要填授权码而不是登录密码；服务器商封了 465/587 端口（换另一个端口试）"
ok "测试通知已发出，请主人确认收到了（邮件可能在垃圾箱里）"

if [ "$CHECK_ONLY" = 1 ]; then
    echo
    echo "检查全部通过（--check：没有安装服务）。"
    exit 0
fi

echo "== 6/6 安装并启动服务 $SERVICE"
ARGS="--cpa-dir \"$CPA_DIR\" --log-dir \"$LOG_DIR\" --state-dir \"$INSTALL_DIR/state\" --base \"$CPA_BASE\""
ARGS="$ARGS --rewrite-model \"$REWRITE\" --hourly-budget ${HOURLY_BUDGET:-10} --daily-budget ${DAILY_BUDGET:-24}"
[ "${REPORT_ONLY:-}" = 1 ] && ARGS="$ARGS --no-apply"
cat > "/etc/systemd/system/$SERVICE.service" <<UNIT
[Unit]
Description=cpa_guard: handle CPA Antigravity fake 429
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=$ENVF
ExecStart=$PY -u "$INSTALL_DIR/cpa_guard.py" $ARGS
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"
sleep 5
if ! systemctl is-active --quiet "$SERVICE"; then
    journalctl -u "$SERVICE" -n 20 --no-pager || true
    fail "服务没能启动，原因见上面的日志"
fi
ok "服务正在运行"

echo
echo "安装完成。cpa_guard 会一直在后台运行，开机自动启动；发现假 429 会自己处理，并发通知。"
echo "  看运行日志：  journalctl -u $SERVICE -f"
echo "  看状态预算：  $PY $INSTALL_DIR/cpa_guard.py --state-dir $INSTALL_DIR/state --cpa-dir \"$CPA_DIR\" --status"
echo "  处理记录：    $INSTALL_DIR/state/events.log"
