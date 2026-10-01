#!/usr/bin/env bash
# Full backend verification, one command:  bash tools/verify.sh   (≈ 10 min)
#   1. unit/smoke tests      2. independent algorithm audit
#   3. headless stress fuzz  4. live dispatcher scenario through API + WS on its own server
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
PORT=${VERIFY_PORT:-8010}
fail=0

echo "== 1/4  pytest"
$PY -m pytest -q 2>&1 | tail -1 | tee /tmp/verify_pytest.txt
grep -q "failed" /tmp/verify_pytest.txt && fail=1

echo; echo "== 2/4  аудит алгоритма (независимая проверка планов + сравнение с наивным диспетчером)"
$PY -m tools.audit --cases ${AUDIT_CASES:-20} --procs 3 2>/dev/null | sed -n '/Итог/,$p' | tee /tmp/verify_audit.txt
grep -q "нарушений правил: 0" /tmp/verify_audit.txt || fail=1

echo; echo "== 3/4  стресс: случайные сбои × 5 стилей поведения диспетчера"
$PY -m tools.stress --runs ${STRESS_RUNS:-40} --procs 8 --timeout 3000 2>/dev/null | grep -E "^FAIL|passed" | tee /tmp/verify_stress.txt
grep -q "^FAIL" /tmp/verify_stress.txt && fail=1

echo; echo "== 4/4  живой сценарий диспетчера через API и WebSocket (сервер на :$PORT, ×60)"
PORT=$PORT $PY -c "from app import all as m; m.main()" > /tmp/verify_server.log 2>&1 &
SRV=$!
for _ in $(seq 1 30); do curl -sf "localhost:$PORT/health" >/dev/null && break; sleep 1; done
$PY -u -m tools.e2e_dispatcher --speed 60 --base "http://127.0.0.1:$PORT" 2>&1 | grep -E "^#|FAIL|ok, " | tee /tmp/verify_e2e.txt
kill $SRV 2>/dev/null
grep -qE "^ +FAIL " /tmp/verify_e2e.txt && fail=1
grep -qE " 0 FAIL " /tmp/verify_e2e.txt || fail=1  # the summary line must say 0 FAIL
echo "   ошибок в логе сервера: $(grep -cE 'ERROR|Traceback' /tmp/verify_server.log)"

echo
if [ $fail -eq 0 ]; then echo "ИТОГ: ВСЁ ЗЕЛЁНОЕ"; else echo "ИТОГ: ЕСТЬ ПРОВАЛЫ — см. выше"; fi
exit $fail
