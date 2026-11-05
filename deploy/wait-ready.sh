#!/bin/sh
# Wait until the loopback model services answer, so they start in the documented order:
# vLLM first, then speech, then the gateway. Usage: wait-ready.sh vlm | speech | all
# Gives up after 900 seconds so a broken service fails visibly instead of hanging.
set -eu
want=${1:-all}
deadline=$(( $(date +%s) + 900 ))
vlm_ready() { curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:8081/v1/models 2>/dev/null | grep -q retail-nemotron; }
speech_ready() { curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:8082/health 2>/dev/null | grep -q '"ready":true'; }
while :; do
  ok=1
  case "$want" in
    vlm) vlm_ready || ok=0 ;;
    speech) speech_ready || ok=0 ;;
    all) { vlm_ready && speech_ready; } || ok=0 ;;
    *) echo "usage: wait-ready.sh vlm|speech|all" >&2; exit 2 ;;
  esac
  [ "$ok" -eq 1 ] && exit 0
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "wait-ready: $want not ready after 900 s" >&2
    exit 1
  fi
  sleep 5
done
