#!/bin/bash
# Run cpa_guard.py once against the fake CPA.
# usage: ./run.sh NAME TRIGGERS_JSON normal|tos PATCH_MODE SYSTEM_PROMPT_FILE [extra cpa_guard.py args]
#   PATCH_MODE: replace | ignore | append | dropwords | timeout | getfail
#   REWRITER=echo ./run.sh ...   makes the fake translator return the line unchanged
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
NAME=$1; TRIG=$2; MODE=$3; PATCH=$4; SYS=$5; shift 5
D=${TMPDIR:-/tmp}/cpa-guard-tests/$NAME
rm -rf "$D"; mkdir -p "$D/cpa" "$D/logs" "$D/state"
printf 'api-keys:\n  - "k1"\nrouting:\n  retry:\n    request-retry: 1\n' > "$D/cpa/config.yaml"
echo "{\"triggers\":$TRIG,\"mode\":\"$MODE\",\"patch\":\"$PATCH\",\"rewriter\":\"${REWRITER:-ok}\"}" > "$D/fake.json"
PORT=$((19000 + RANDOM % 900))
python3 "$HERE/fake_cpa.py" "$D/fake.json" $PORT & FP=$!
trap 'kill $FP 2>/dev/null' EXIT
sleep 0.7
LOG="$D/logs/error-v1-chat-completions-test.log"
python3 - "$SYS" "$LOG" <<'PY'
import json, sys
system = open(sys.argv[1]).read()
body = json.dumps({"model": "gemini-3.8-flash-high",
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": "hi"}],
                   "tools": [{"type": "function", "function": {"name": "read_file"}},
                             {"type": "function", "function": {"name": "terminal"}}]})
open(sys.argv[2], "w").write("=== REQUEST INFO ===\nURL: /v1/chat/completions\n\n=== HEADERS ===\nUser-Agent: test\n\n"
                             "=== REQUEST BODY ===\n" + body + "\n\n=== RESPONSE ===\nStatus: 429\n"
                             "{\"error\":{\"status\":\"RESOURCE_EXHAUSTED\"}}\n")
PY
CPA_API_KEY=k1 CPA_MGMT_KEY=m1 python3 "$HERE/../cpa_guard.py" "$@" --cpa-dir "$D/cpa" --log-dir "$D/logs" \
  --state-dir "$D/state" --base http://127.0.0.1:$PORT --pause 0 --cooldown-wait 0 --reload-wait 0.2 --once "$LOG"
echo "--- fake CPA:"
curl -s http://127.0.0.1:$PORT/dump | python3 -c "
import json, sys
d = json.load(sys.stdin)
print('probes', d['calls'].count('PROBE'), 'patches', sum(c.startswith('PATCH') for c in d['calls']),
      'word rules present', d['words'])
for p in d['patterns']:
    print('  rule:', p['pattern'][:60], '->', p['replacement'][:60])"
echo "--- guard state:"
python3 -c "
import json; d = json.load(open('$D/state/state.json'))
print('429 probes', len(d['hits']), 'results', {k: v['status'] for k, v in d['handled'].items()}, 'parked', list(d['pending']),
      'halted', bool(d['halted']), 'apply_disabled', bool(d['apply_disabled']), 'unverified', d['unverified'])"
