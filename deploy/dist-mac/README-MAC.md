# Turb GPT Free Register — bản chạy cho macOS

Gói này để chạy WebUI trên máy Mac mới. Giải nén, double-click `start.command` là xong.

## Yêu cầu trên máy Mac

- **Python 3.10+** — tải từ <https://www.python.org/downloads/> (bản cho Apple Silicon hoặc Intel), hoặc `brew install python`
- **Node.js 18+** — cần cho luồng đăng ký dùng Sentinel/PoW: `brew install node` hoặc tải từ <https://nodejs.org>
- Internet lần đầu chạy (để cài dependencies, ~vài phút)

## Chạy

1. Giải nén `turb-gpt-free-register-mac.zip` → được thư mục `turb-gpt-free-register-mac`.
2. **Nếu macOS chặn file tải về** (báo "cannot verify the developer" / "permission denied"):
   - Chuột phải vào `start.command` → **Open** → Open (làm 1 lần), hoặc
   - Chạy trong Terminal: `xattr -dr com.apple.quarantine /đường/dẫn/turb-gpt-free-register-mac`
3. Double-click `start.command`:
   - Lần đầu: tự tạo `.venv`, tự cài dependencies, tự tạo `.env` từ `.env.example`; có thể hỏi cài Chromium cho Playwright (~200MB, chọn No nếu không dùng luồng Playwright).
   - Các lần sau: chạy nhanh, tự mở trình duyệt tại **http://127.0.0.1:5057**
4. **Mã đăng nhập WebUI:** nếu `WEBUI_AUTH_CODE` trong `.env` đang trống, một mã tạm được in trong cửa sổ Terminal lúc khởi động — dùng mã đó để đăng nhập, hoặc điền `WEBUI_AUTH_CODE` vào `.env` để cố định.
5. **Dừng WebUI:** đóng cửa sổ Terminal, hoặc double-click `stop.command`.

Đổi cổng/host: sửa `PORT=5057` / `HOST=127.0.0.1` ở đầu file `start.command`. Mặc định WebUI chỉ lắng nghe 127.0.0.1 (không lộ ra LAN) — nên giữ nguyên.

## Chuyển dữ liệu từ máy cũ (tuỳ chọn)

WebUI tắt hoàn toàn rồi copy các file sau vào thư mục app trên Mac:

| File | Nội dung |
| --- | --- |
| `.env` | Cấu hình + secret bootstrap |
| `turb.sqlite3` | Trạng thái công việc, cấu hình đã lưu trong WebUI |
| `用于注册的*.json` / `用于注册的*.txt` | Pool hộp thư dùng để đăng ký |
| `注册成功的*.json` / `注册成功的*.txt` | Danh sách tài khoản đã đăng ký thành công |

⚠️ Các file này chứa **thông tin nhạy cảm** (mật khẩu, token, tài khoản) — chỉ copy qua kênh riêng tư, không chia sẻ, không commit.

## Lỗi thường gặp

- **"permission denied" khi double-click:** trong Terminal chạy `chmod +x start.command stop.command` rồi thử lại.
- **"Không tìm thấy Python 3.10+"**: cài Python theo link ở trên rồi chạy lại. Lưu ý Python hệ thống cũ (3.9 trở xuống từ Command Line Tools) không đủ.
- **Không cài được dependencies:** kiểm tra mạng, xoá thư mục `.venv` rồi double-click lại để cài từ đầu.
- **Luồng đăng ký báo lỗi Sentinel:** máy chưa có Node.js 18+ — `brew install node`.
- **CloakBrowser/Playwright tải browser lần đầu chậm:** cần mạng ổn định, chỉ xảy ra một lần.

## Ghi chú kỹ thuật

`start.command` làm các việc sau mỗi lần chạy: tìm Python 3.10+ → tạo `.venv` (lần đầu) → so khớp sha256 của `requirements.txt` với stamp trong `.venv` để quyết định có cài lại dependencies không → tạo `.env` nếu thiếu → kiểm tra Node.js → hỏi cài Chromium nếu chưa có → kiểm tra cổng (nếu WebUI đang chạy thì chỉ mở trình duyệt) → chạy `web.py --host 127.0.0.1 --port 5057 --open-browser` ở foreground. WebUI có khoá single-instance theo cổng nên double-click nhiều lần không tạo bản chạy trùng.
