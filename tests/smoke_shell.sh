#!/usr/bin/env bash
# Live smoke test against the running omarchy shell: the service is loaded,
# the IPC target answers, the windows open and close, and the shell log has no
# cartridge warning. The QML wiring has no unit tests (there is no test runner
# with Omarchy's types outside the shell), so this is its coverage.
#
#   make smoke            # needs the plugin enabled in a running shell
#   SMOKE_REFRESH=1 make smoke   # also rescans the real library
set -u
PLUGIN=audryus.cartridge
fail=0
ok()   { echo "ok   $1"; }
bad()  { echo "FAIL $1"; fail=1; }

wait_for() {   # wait_for <seconds> <command...>
    local limit=$1; shift
    local start=$SECONDS
    until "$@" >/dev/null 2>&1; do
        (( SECONDS - start >= limit )) && return 1
        sleep 0.3
    done
}

status() { omarchy-shell "$PLUGIN" status 2>/dev/null; }

instance=$(ls -t "/run/user/$(id -u)/quickshell/by-id/" 2>/dev/null | head -1)
[[ -n $instance ]] || { echo "no running quickshell instance"; exit 2; }
# Warnings and errors only: the scanner's progress is logged as info.
problems() { qs log -i "$instance" 2>/dev/null | grep -iE "WARN|ERROR|CRIT" | grep -i cartridge; }
before=$(problems | wc -l)

if wait_for 10 sh -c "omarchy-shell $PLUGIN status | grep -q '\"shared\":true'"; then
    ok "the shared service owns the IPC target"
else
    bad "status did not come from the shared service: $(status)"
fi

omarchy-shell "$PLUGIN" open
if wait_for 10 sh -c "omarchy-shell $PLUGIN status | grep -qv '\"roms\":0'"; then
    ok "open loads the library: $(status)"
else
    bad "the library did not load: $(status)"
fi
omarchy-shell "$PLUGIN" config && ok "config opens"
omarchy-shell "$PLUGIN" close && ok "close"

if [[ ${SMOKE_REFRESH:-0} == 1 ]]; then
    omarchy-shell "$PLUGIN" refresh
    wait_for 5 sh -c "omarchy-shell $PLUGIN status | grep -q '\"scanning\":true'"
    if wait_for 300 sh -c "omarchy-shell $PLUGIN status | grep -q '\"scanning\":false'"; then
        ok "refresh ran the scanner: $(status)"
    else
        bad "the scan did not finish"
    fi
    omarchy-shell "$PLUGIN" close
fi

after=$(problems | wc -l)
if (( after == before )); then
    ok "no cartridge warnings in the shell log"
else
    bad "new cartridge warnings:"
    problems | tail -n $(( after - before ))
fi
exit $fail
