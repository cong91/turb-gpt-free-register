# Gmail API URL Multi-Alias Batch

## Tổng quan

Feature cho phép mỗi email gốc trong pool sinh nhiều alias (tối đa 12), tất cả alias share cùng `code_url` của email gốc. Luồng đăng ký `gmail_api_url` tạo một canonical lazy batch cho mỗi lane; WebUI cố định 12 alias/source và mỗi lane chỉ mua source tiếp theo sau khi đã dùng hết alias của source hiện tại. Tài liệu API thấp hơn bên dưới chỉ mô tả helper canonical cấp thấp, không phải một nguồn đăng ký QAN8 riêng.

## Cơ chế

### Cấu trúc alias
Mỗi email gốc `user@gmail.com` sinh tối đa 12 alias:
- 6 alias `@gmail.com` và 6 alias `@googlemail.com` (email gốc không được đưa lại vào danh sách alias)
- Toàn bộ chuỗi chỉ có tối đa 1 local-part chứa dấu chấm; các alias còn lại dùng hậu tố `+...`

### Batch flow
1. **Batch creation**: Tính số source group và tạo một canonical batch rỗng
   cho mỗi logical lane.
   - WebUI: `count` là số source group; backend tạo `count × 12` job.
   - Sub2API: `count` là số job tài khoản; source group cần `ceil(count / 12)`.
   - `lane_count = min(workers, source_group_count)`. Source group thứ `i`
     thuộc lane `i % lane_count`; lane giữ tuần tự toàn bộ 12 alias của source
     đó trước khi chuyển source kế tiếp.

2. **Source materialization**: Ưu tiên source Gmail đã import và còn alias claim được. Khi queue rỗng, hệ thống materialize đúng một source vào batch:
   ```python
   # Một source có tối đa 12 alias, tất cả dùng chung code_url.
   # Nếu pool local hết source thì purchase adapter QAN8 mua quantity=1.
   provision_next_gmail_api_url_source(batch_id, aliases_per_source=12)
   ```

3. **Job claim**: Mỗi lane chỉ claim alias từ batch của chính nó, nhận
   `code_url` của source tương ứng. Không có fallback sang batch khác. Hết 12
   alias của source hiện tại, lane đó mới materialize hoặc mua source tiếp theo
   nếu vẫn còn job trong lane.

### Rollback
Nếu batch creation thất bại (pool không đủ email, hoặc sinh alias lỗi), tất cả email gốc đã claim được release về pool với status `available`.

## API

### Low-level canonical helper

`create_registration_batch` vẫn có mặt cho các tác vụ cấp thấp cần dựng batch
từ các source đã import. Nó không được WebUI hoặc Sub2API registration path gọi;
luồng đăng ký chính dùng `create_empty_batch` và provision lazy.

```python
from core.gmail_api_url_client import create_registration_batch

# Tạo batch 30 alias, mỗi email gốc sinh 12 alias → claim 3 email từ pool
batch_id = create_registration_batch(count=30, aliases_per_email=12)

# Claim alias cho job
account = get_email_from_batch(batch_id, job_id="job0")
# account.email: "user1@gmail.com"
# account.code_url: "https://gapi.mailsapi.com/.../abc123"
```

### Web UI

POST `/api/jobs/registration/submit`:
```json
{
  "count": 30,
  "email_source": "gmail_api_url",
  "workers": 5
}
```

- `count`: số source Gmail cần dùng/mua trên WebUI; backend tự tạo `count × 12` job
- Alias count được cố định ở 12 cho source Gmail API URL; không có option `qan8_gmail_api` hay cấu hình alias riêng

## Database Schema

### batches table
```sql
CREATE TABLE batches (
    batch_id TEXT PRIMARY KEY,
    source_email TEXT NOT NULL,  -- Email gốc của group đầu tiên (tương thích cũ)
    code_url TEXT NOT NULL,      -- code_url của group đầu tiên
    created_at REAL NOT NULL,
    total_count INTEGER NOT NULL
)
```

### assignments table
```sql
CREATE TABLE assignments (
    assignment_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    code_url TEXT NOT NULL,      -- code_url thực của alias này
    job_id TEXT,
    status TEXT NOT NULL,         -- 'available' | 'claimed'
    claimed_at REAL,
    updated_at REAL NOT NULL,
    UNIQUE(batch_id, alias)
)
```

## Testing

```bash
python -m unittest tests.test_gmail_api_url_batch
```

8 tests cover:
- Single source: all aliases share same code_url
- Multi-source: aliases keep per-group code_url
- Pool exhaustion rollback
- Claim idempotence
- Batch exhaustion detection

## Ví dụ

### Ví dụ WebUI: dùng 3 source Gmail

# WebUI count là số source group, không phải số alias.
jobs = submit_registration(
    count=36,
    email_source="gmail_api_url",
    gmail_api_url_aliases_per_email=12,
    workers=20
)
```

Batch plan với 3 worker: 3 batch độc lập, mỗi batch có 12 job và 1 source
group. Mỗi source tối đa 12 alias share 1 `code_url`; ba lane có thể chạy song
song. Nếu có 5 source group và 3 worker, các lane lần lượt có plan 24, 24, 12
job và mỗi lane chỉ mua source kế tiếp sau khi dùng hết 12 alias hiện tại.
QAN8 chỉ được gọi khi alias local của lane đó đã hết và lane đó còn job chờ.

### Rollback khi pool không đủ

```python
# Helper cấp thấp chỉ dùng khi cần dựng batch eager từ pool local.
# Pool thiếu source sẽ raise GmailApiUrlBatchError và rollback claim.
try:
    batch_id = create_registration_batch(count=36, aliases_per_email=12)
except GmailApiUrlBatchError as exc:
    print(exc)
```

## Lưu ý

1. **Alias limit**: Mỗi email Gmail chỉ sinh tối đa 12 alias (6 gmail.com + 6 googlemail.com), tối đa 1 alias có dấu chấm rồi bù bằng alias `+...`
2. **Pool management**: Email gốc vẫn ở status `claimed` sau khi tạo batch; chỉ release khi batch hoàn thành hoặc failed
3. **Runtime source**: `gmail_api_url` là nguồn đăng ký duy nhất; QAN8 chỉ là purchase adapter và luôn mua `quantity=1`
4. **Code URL sharing**: Tất cả alias trong cùng group share code_url của email gốc → poll verification code từ cùng 1 endpoint; một `code_url` chỉ thuộc một canonical lane batch
