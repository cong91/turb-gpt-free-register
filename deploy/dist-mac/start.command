#!/bin/bash
# Turb GPT Free Register — WebUI launcher cho macOS
#
# Double-click file này trong Finder để chạy WebUI.
# Lần đầu chạy: tự tạo .venv, cài dependencies, tạo .env từ .env.example.
#
# Cấu hình nhanh: sửa HOST / PORT bên dưới nếu cần.
set -u

HOST="127.0.0.1"
PORT=5057
URL="http://${HOST}:${PORT}"

cd "$(dirname "$0")" || exit 1
ROOT_DIR="$(pwd)"
VENV_DIR="$ROOT_DIR/.venv"

printf '\033]0;Turb GPT Free Register\007'

say() { printf '%s\n' "$*"; }

fail() {
  say ""
  say "❌ $*"
  say ""
  read -r -p "Nhấn Enter để đóng cửa sổ..." _
  exit 1
}

# Tìm Python 3.10+ trong PATH hoặc các vị trí phổ biến trên macOS
find_python() {
  local candidate
  for candidate in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
        command -v "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

say "=============================================="
say " Turb GPT Free Register — WebUI"
say " Thư mục: $ROOT_DIR"
say "=============================================="
say ""

PY="$(find_python)"
if [ -z "$PY" ]; then
  fail "Không tìm thấy Python 3.10+ trên máy.

Cài Python trước theo một trong hai cách:
  • Tải từ https://www.python.org/downloads/ (chọn bản cho Apple Silicon hoặc Intel)
  • Hoặc nếu có Homebrew:  brew install python

Sau đó chạy lại start.command."
fi
say "[OK] Python $("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])'): $PY"

# Tạo .venv nếu chưa có
if [ ! -x "$VENV_DIR/bin/python" ]; then
  say "[SETUP] Đang tạo môi trường ảo .venv (chỉ lần đầu)..."
  "$PY" -m venv "$VENV_DIR" || fail "Không tạo được .venv."
fi
VENV_PY="$VENV_DIR/bin/python"
"$VENV_PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
  || fail ".venv hiện tại dùng Python cũ hơn 3.10. Xoá thư mục .venv rồi chạy lại start.command."

# Cài dependencies khi requirements.txt thay đổi (so khớp sha256, giống start-local.ps1)
REQ_HASH="$(shasum -a 256 "$ROOT_DIR/requirements.txt" 2>/dev/null | awk '{print $1}')"
STAMP="$VENV_DIR/.requirements.sha256"
INSTALLED_HASH=""
[ -f "$STAMP" ] && INSTALLED_HASH="$(head -n 1 "$STAMP" 2>/dev/null)"

if [ "$REQ_HASH" != "$INSTALLED_HASH" ]; then
  say "[SETUP] Đang cài Python dependencies (lần đầu có thể mất vài phút, vui lòng chờ)..."
  "$VENV_PY" -m pip install --upgrade pip || fail "Không nâng cấp được pip."
  "$VENV_PY" -m pip install -r "$ROOT_DIR/requirements.txt" \
    || fail "Không cài được dependencies. Kiểm tra kết nối mạng rồi chạy lại start.command."
  printf '%s\n' "$REQ_HASH" > "$STAMP"
else
  say "[OK] Python dependencies sẵn sàng."
fi

"$VENV_PY" -m pip check \
  || fail "Dependencies thiếu hoặc xung đột. Xoá thư mục .venv rồi chạy lại start.command."

# Tạo .env từ .env.example nếu chưa có
if [ ! -f "$ROOT_DIR/.env" ]; then
  cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
  say "[SETUP] Đã tạo .env từ .env.example."
fi

# Node.js 18+ cần cho luồng đăng ký dùng Sentinel/PoW — thiếu thì cảnh báo, không chặn WebUI
NODE_MAJOR=0
if command -v node >/dev/null 2>&1; then
  NODE_MAJOR="$(node --version 2>/dev/null | sed 's/^v//' | cut -d. -f1 | tr -dc '0-9')"
  [ -z "$NODE_MAJOR" ] && NODE_MAJOR=0
fi
if [ "$NODE_MAJOR" -ge 18 ]; then
  say "[OK] Node.js $(node --version)"
else
  say "⚠️  Chưa có Node.js 18+: luồng đăng ký dùng Sentinel/PoW sẽ không chạy được."
  say "    Cài đặt: brew install node   (hoặc tải từ https://nodejs.org)"
  printf '%s' "Vẫn tiếp tục chạy WebUI? [Y/n] "
  read -r ANSWER
  if [ "$ANSWER" = "n" ] || [ "$ANSWER" = "N" ]; then
    fail "Đã dừng. Cài Node.js 18+ rồi chạy lại start.command."
  fi
fi

# Chromium cho Playwright (chỉ hỏi khi chưa có và chưa từng bỏ qua)
PW_CACHE="$HOME/Library/Caches/ms-playwright"
PW_SKIP_STAMP="$VENV_DIR/.playwright.skip"
if ! ls "$PW_CACHE" >/dev/null 2>&1 && [ ! -f "$PW_SKIP_STAMP" ]; then
  say ""
  say "Playwright cần Chromium (~200MB tải về) cho các luồng trình duyệt."
  printf '%s' "Cài Chromium cho Playwright ngay? [Y/n] "
  read -r ANSWER
  if [ "$ANSWER" = "n" ] || [ "$ANSWER" = "N" ]; then
    printf 'skipped\n' > "$PW_SKIP_STAMP"
    say "[SKIP] Bỏ qua. Cài sau bằng: \"$VENV_PY\" -m playwright install chromium"
  else
    "$VENV_PY" -m playwright install chromium \
      || say "⚠️  Không cài được Chromium. Có thể bỏ qua nếu không dùng luồng Playwright."
  fi
fi

# Cổng đã có ai giữ chưa
LISTENER_PID="$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null | head -n 1)"
if [ -n "$LISTENER_PID" ]; then
  PROC_CMD="$(ps -p "$LISTENER_PID" -o command= 2>/dev/null)"
  case "$PROC_CMD" in
    *web.py*)
      say "[OK] WebUI đang chạy sẵn (PID $LISTENER_PID). Mở trình duyệt..."
      open "$URL"
      sleep 1
      exit 0
      ;;
    *)
      fail "Cổng $PORT đang bị tiến trình khác giữ:
  $PROC_CMD
Hãy tắt tiến trình đó, hoặc sửa PORT trong start.command."
      ;;
  esac
fi

say ""
say "[START] WebUI: $URL"
say "        Đóng cửa sổ Terminal này (hoặc chạy stop.command) để dừng WebUI."
say ""
export PYTHONUTF8=1
"$VENV_PY" web.py --host "$HOST" --port "$PORT" --open-browser
EXIT_CODE=$?

if [ "$EXIT_CODE" -ne 0 ]; then
  say ""
  say "❌ WebUI đã dừng với mã lỗi $EXIT_CODE. Xem thông báo phía trên."
  say "   Có thể chạy thủ công để xem log chi tiết: \"$VENV_PY\" web.py --verbose"
  read -r -p "Nhấn Enter để đóng cửa sổ..." _
fi
exit "$EXIT_CODE"
