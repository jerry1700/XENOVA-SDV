#!/bin/sh
# 게이트웨이를 멈추고 중계 도구를 돌린 뒤 게이트웨이를 다시 켠다. 게이트웨이가 시리얼을 쥐고 있으면 중계 도구가 열지 못한다.
#   pi_bridge_run.sh <출력 파일> <pi_bridge.py 인자...>
# pi_bridge.py, fb3.py, link_bench.py 와 같은 폴더에 두고 그 폴더에서 실행한다.
OUT=$1; shift
HERE=$(cd "$(dirname "$0")" && pwd)
restore() {
  systemctl is-active --quiet xenova-gateway || systemctl start xenova-gateway
  sleep 3
  echo "gateway-after=$(systemctl is-active xenova-gateway)" >> "$OUT"
  rm -f "$HERE/bridge.pid"
}
echo $$ > "$HERE/bridge.pid"
: > "$OUT"
systemctl stop xenova-gateway
if systemctl is-active --quiet xenova-gateway; then echo "GATEWAY STILL ACTIVE" >> "$OUT"; rm -f "$HERE/bridge.pid"; exit 1; fi
trap restore EXIT
trap 'exit 1' INT TERM HUP
cd "$HERE" && python3 pi_bridge.py --port /dev/ttyUSB0 "$@" >> "$OUT" 2>&1
