# Cấu hình Vercel, API key và đăng nhập Google Sheet

Vercel giữ các secret và chạy API; tab `Student` hiện có chứa thông tin tài
khoản và mật khẩu dạng hash. Không đưa API key vào ứng dụng Windows.

## Luồng hoạt động

1. Người dùng nhập username hoặc email, mật khẩu và role trong ứng dụng.
2. Ứng dụng gửi thông tin đăng nhập đến `/api/login` trên Vercel qua HTTPS.
   Vercel đọc tab `Student`, tìm username/email, kiểm tra scrypt hash, role và
   trạng thái.
3. Vercel cấp JWT ký bằng `APP_SESSION_SECRET`, sống trong 4 giờ. Ứng dụng giữ
   token trong SQLite cục bộ và dùng token khi gọi API Google/AI.
4. Vercel gọi Gemini, Groq, Google Sheets và Google Drive bằng credentials chỉ
   lưu trong Environment Variables.
5. File audio/video được ứng dụng chuyển đến phiên resumable upload trên Google
   Drive. Vercel xác nhận chủ sở hữu file, đặt quyền xem qua liên kết và đọc
   file Drive để gọi Groq transcription.

> Bản desktop có thể bị phân tích hoặc giả lập. Không có cách đáng tin cậy để
> chứng minh server request chỉ đến từ EXE chính chủ. Bảo vệ API bằng mật khẩu,
> quyền `role`, giới hạn chi phí và theo dõi usage; không dựa vào bí mật nhúng
> trong ứng dụng.

## A. Dùng tab `Student` hiện có

Không cần tạo sheet `Users`. Giữ các cột dữ liệu hiện có trong tab `Student` và
đảm bảo có các header:

```text
full_name | username | email | password | role | class_name | status
```

- Bạn đã thêm cột `password` sau `username`; có thể giữ vị trí đó. Cột phải chứa
  **scrypt hash**, không phải mật khẩu dạng rõ.
- Mỗi dòng tài khoản cần có `username`, `email`, `role`, `password`, `status`.
- `role` dùng `student` hoặc `teacher`. Dòng giáo viên bạn đã thêm sẽ được giữ
  lại khi ứng dụng đồng bộ danh sách Student.
- `status` dùng `active` để cho phép đăng nhập, hoặc `locked` để khóa.
- Email và role của mỗi dòng phải trùng với hồ sơ tài khoản cục bộ trong app.
- Khi đồng bộ sheet, ứng dụng giữ lại hash mật khẩu và các dòng giáo viên.

### Tạo mật khẩu hash

Chạy trên máy tin cậy tại thư mục repo cho mỗi tài khoản:

```powershell
py tools\generate_password_hash.py
```

Nhập mật khẩu hai lần tại prompt. Mật khẩu không được echo ra terminal. Dán
hash được in vào cột `password` của đúng dòng trong tab `Student`. Lặp lại với
tài khoản giáo viên. Không ghi mật khẩu dạng rõ vào Sheet và không commit hash
hay mật khẩu vào GitHub.

## B. Tạo Vercel project

1. Push mã nguồn lên repo private GitHub `CVA`.
2. Đăng nhập [Vercel](https://vercel.com) → **Add New → Project** → import repo
   private `CVA`.
3. Trong **Root Directory**, chọn thư mục `vercel` (không chọn thư mục gốc
   repo). Chọn **Other** nếu Vercel yêu cầu framework preset.
4. Thư mục `vercel` chứa riêng API TypeScript, thư viện dùng chung và cấu hình
   Vercel. Nó không chứa `app.py` nên Vercel không thể khởi chạy ứng dụng
   desktop Python làm Function.
5. Giữ `vercel/.vercelignore` cùng các file bên trong `vercel/`. Sau khi push
   thay đổi, vào **Settings → Build and Deployment** xác nhận **Root Directory**
   là `vercel`, rồi redeploy một deployment mới từ commit vừa push.

## C. Đặt Environment Variables

Mở Vercel project → **Settings → Environment Variables**. Thêm các biến cho
Production (thêm Preview chỉ khi thật sự cần dùng tài khoản/dữ liệu thử):

| Tên biến | Nội dung |
|---|---|
| `APP_SESSION_SECRET` | Chuỗi ngẫu nhiên bí mật, tối thiểu 32 ký tự |
| `GEMINI_API_KEY` | Gemini API key |
| `GROQ_API_KEY` | Groq API key |
| `GROQ_EVAL_MODEL` | (Tùy chọn) Model Groq chấm điểm; mặc định `llama-3.3-70b-versatile` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Toàn bộ JSON service account |
| `GOOGLE_SHEET_ID` | ID Google Spreadsheet |
| `GOOGLE_DRIVE_FOLDER_ID` | ID thư mục Drive lưu bài nộp |

Tạo secret phiên bằng PowerShell với Python cài trên máy:

```powershell
py -c "import secrets; print(secrets.token_urlsafe(48))"
```

Chỉ dán output vào Environment Variable `APP_SESSION_SECRET`. Không commit,
không gửi qua chat và không đưa vào `cloud-config.json`.

Với `GOOGLE_SERVICE_ACCOUNT_JSON`, mở file JSON cục bộ, sao chép toàn bộ nội
dung `{ ... }` vào Vercel. Không upload file JSON lên GitHub. Chia sẻ Google
Spreadsheet và thư mục Drive cho `client_email` trong JSON, cấp quyền Editor.
Google Sheets API và Google Drive API cần bật cho project Google Cloud thuộc
service account.

Lưu biến, rồi **Redeploy** để deployment nhận các giá trị mới. Nếu secret từng
bị lộ, tạo secret/API key mới ở nhà cung cấp và cập nhật Vercel; chỉ xóa key
trong code không thu hồi key cũ.

## D. Cấu hình URL Vercel

Sau khi deploy, sao chép domain production dạng
`https://<ten-project>.vercel.app`. Sửa `static/js/cloud-config.json`:

```json
{
  "vercel_api_base_url": "https://<ten-project>.vercel.app"
}
```

Đây là URL công khai, không phải secret. Commit/push cấu hình này và build lại
ứng dụng desktop để URL được đóng gói vào bản phát hành.

## E. Kiểm tra đăng nhập và API

1. Đảm bảo tài khoản có cùng username, email và role trong tab `Student` và
   dữ liệu tài khoản cục bộ của ứng dụng.
2. Mở ứng dụng desktop và đăng nhập. Tài khoản không tồn tại, sai mật khẩu,
   role không khớp hoặc có trạng thái `locked` phải bị từ chối.
3. Giáo viên thử đọc/ghi các sheet `Student`, `Lesson`, `Practice`; học sinh
   thử nộp bài và không được phép chạy chức năng giáo viên.
4. Nộp audio/video nhỏ. Kiểm tra file ở đúng folder Google Drive; bài audio
   phải được đọc qua Drive và gửi đến Groq để tạo transcript.
5. Xem Vercel Runtime Logs để tìm lỗi. Không bật logging của password, JWT,
   Google credentials hoặc API key.

## Xử lý sự cố

- **Đăng nhập báo lỗi dịch vụ:** kiểm tra URL trong `cloud-config.json`, Vercel
  deployment và kết nối mạng.
- **Vercel Logs báo `could not import "app.py"` hoặc
  `sqlite3.OperationalError: unable to open database file`:** Vercel đang chạy
  nhầm ứng dụng desktop Python thay vì các API TypeScript. Vào **Settings →
  Build and Deployment**, đổi **Root Directory** thành `vercel` (không phải
  repo root), lưu lại và redeploy từ commit mới nhất có thư mục `vercel/`.
  Kiểm tra `vercel/.vercelignore` có được đẩy lên GitHub. Khi deploy đúng,
  request POST thiếu dữ liệu tới `/api/login` phải trả lỗi JSON HTTP 400,
  không phải `FUNCTION_INVOCATION_FAILED`.
- **Đăng nhập bị từ chối:** kiểm tra username/email, scrypt hash trong cột
  `password`, role, `active` trong status và hồ sơ email/role cục bộ.
- **Vercel trả 503:** kiểm tra tên các Environment Variables và redeploy.
- **Google trả 403:** xác nhận Sheets/Drive API đã bật và service account có
  quyền Editor trên Sheet/folder; kiểm tra Sheet ID và Drive folder ID.
- **Upload/transcription thất bại:** kiểm tra Drive folder ID, quyền service
  account, file nhỏ hơn 18 MB, biến `GROQ_API_KEY` và Vercel logs.
- **JWT hết hạn:** đăng xuất rồi đăng nhập lại. Token được đặt thời hạn 4 giờ.

## Bảo mật và giới hạn

- Mật khẩu chỉ được gửi qua HTTPS đến Vercel để xác thực, không lưu trong local
  cloud token table. Sheet chỉ lưu scrypt hash. Dùng mật khẩu mạnh và không tái
  sử dụng mật khẩu cá nhân.
- Thay `APP_SESSION_SECRET` sẽ vô hiệu hóa mọi JWT đã cấp trước đó.
- Bộ giới hạn request trong Vercel hiện giữ trạng thái trong bộ nhớ của Function;
  serverless có thể tạo nhiều instance hoặc khởi động lại, vì vậy giới hạn này
  chỉ best-effort, không phải quota bền vững/chống abuse hoàn chỉnh.
- Ứng dụng desktop không thể giấu hoàn toàn endpoint hoặc ngăn người dùng tự
  gọi API. Dùng hạn mức/billing alerts tại Vercel, Google, Gemini và Groq.
- Credential cũ ở các dịch vụ trước đây vẫn tồn tại cho đến khi bạn chủ động
  thu hồi hoặc xóa chúng.

Tham khảo: [Vercel Environment Variables](https://vercel.com/docs/environment-variables),
[Vercel Functions](https://vercel.com/docs/functions) và
[Google Drive resumable uploads](https://developers.google.com/drive/api/guides/manage-uploads).
