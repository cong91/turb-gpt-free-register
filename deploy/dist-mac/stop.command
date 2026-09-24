#!/bin/bash
# Turb GPT Free Register — dừng WebUI đang chạy trên cổng PORT
set -u

PORT=5057

cd "$(dirname "$0")" || exit 1

PIDS="$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null)"
if [ -z "$PIDS" ]; then
  echo "WebUI không chạy trên cổng $PORT."
  sleep 2
  exit 0
fi

echo "Đang dừng WebUI (PID: $(echo "$PIDS" | tr '\n' ' '))..."
for PID in $PIDS; do
  kill "$PID" 2>/dev/null
done

i=0
while [ "$i" -lt 20 ]; do
  REMAIN="$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null)"
  [ -z "$REMAIN" ] && break
  sleep 0.5
  i=$((i + 1))
done

REMAIN="$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null)"
if [ -n "$REMAIN" ]; then
  for PID in $REMAIN; do
    kill -9 "$PID" 2>/dev/null
  done
  echo "Đã buộc dừng tiến trình giữ cổng $PORT."
else
  echo "Đã dừng WebUI."
fi
sleep 2
