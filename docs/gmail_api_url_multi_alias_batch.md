# Gmail API URL Multi-Alias Batch

## Tổng quan

Feature cho phép mỗi email gốc trong pool sinh nhiều alias (tối đa 12), tất cả alias share cùng `code_url` của email gốc. Điều này tối ưu việc sử dụng pool khi có nhiều job đăng ký cùng lúc.

## Cơ chế

### Cấu trúc alias
Mỗi email gốc `user@gmail.com` sinh tối đa 12 alias:
- 6 alias `@gmail.com` và 6 alias `@googlemail.com` (email gốc không được đưa lại vào danh sách alias)
- Toàn bộ chuỗi chỉ có tối đa 1 local-part chứa dấu chấm; các alias còn lại dùng hậu tố `+...`

### Batch flow
1. **Pool claim**: Claim đủ email gốc từ pool để tạo `count` alias.
   - Số email gốc cần = `ceil(count / aliases_per_email)`
   - Mỗi email gốc sinh tối đa `aliases_per_email` alias (1..12)

2. **Batch creation**: Tạo batch với `create_batch_multi(groups)`:
   ```python
   groups = [
       {
           "source_email": "user1@gmail.com",
           "code_url": "https://gapi.mailsapi.com/api/get-code?uid=abc123",
           "aliases": ["user1@gmail.com", "user1+1@gmail.com", ...]
       },
       {
           "source_email": "user2@gmail.com",
           "code_url": "https://gapi.mailsapi.com/api/get-code?uid=def456",
           "aliases": ["user2@gmail.com", "user2+1@gmail.com", ...]
       }
   ]
   ```

3. **Job claim**: Mỗi job claim 1 alias từ batch, nhận `code_url` của email gốc tương ứng.

### Rollback
Nếu batch creation thất bại (pool không đủ email, hoặc sinh alias lỗi), tất cả email gốc đã claim được release về pool với status `available`.

## API

### Backend

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
  "gmail_api_url_alias_count": 12,
  "workers": 5
}
```

- `count`: số email gốc lấy từ pool
- `gmail_api_url_alias_count` (optional, default=1): số alias mỗi email gốc (1..12)
- Nếu = 1: mỗi job claim 1 email gốc riêng (luồng cũ)
- Nếu > 1: backend nhân `count × alias_count` rồi tạo từng job một alias; ví dụ `count=1`, `alias_count=12` → 12 tài khoản

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

### Tạo 100 tài khoản với 10 email gốc

```python
# Mỗi email gốc sinh 10 alias → claim 10 email từ pool
batch_id = create_registration_batch(count=100, aliases_per_email=10)

# Submit 100 job, mỗi job nhận 1 alias
jobs = submit_registration(
    count=100,
    email_source="gmail_api_url",
    gmail_api_url_aliases_per_email=10,
    workers=20
)
```

Pool trước: 50 email available  
Pool sau: 40 email available (10 email claimed)  
Batch: 100 alias, 10 group, mỗi group 10 alias share 1 code_url

### Rollback khi pool không đủ

```python
# Pool chỉ có 5 email, cần 10 email → claim được 5, sau đó raise GmailApiUrlBatchError
# Cả 5 email đã claim được release về pool với status 'available'
try:
    batch_id = create_registration_batch(count=100, aliases_per_email=10)
except GmailApiUrlBatchError as exc:
    # "Gmail API URL pool không đủ email: cần 10 email (mỗi email 10 alias) cho 100 tài khoản, chỉ claim được 5"
    print(exc)
```

## Lưu ý

1. **Alias limit**: Mỗi email Gmail chỉ sinh tối đa 12 alias (6 gmail.com + 6 googlemail.com), tối đa 1 alias có dấu chấm rồi bù bằng alias `+...`
2. **Pool management**: Email gốc vẫn ở status `claimed` sau khi tạo batch; chỉ release khi batch hoàn thành hoặc failed
3. **Backward compatibility**: `aliases_per_email=1` hoặc không truyền → mỗi job claim 1 email gốc riêng (luồng cũ)
4. **Code URL sharing**: Tất cả alias trong cùng group share code_url của email gốc → poll verification code từ cùng 1 endpoint
