# Cấu hình Vercel, API key và đăng nhập Google Sheet

Vercel giữ các secret và chạy API; tab `Student` hiện có chứa thông tin tài
khoản và mật khẩu dạng hash. Không đưa API key vào ứng dụng Windows.

## Luồng hoạt động

1. Người dùng nhập username hoặc email, mật khẩu và role trong ứng dụng.
2. Ứng dụng gửi thông tin đăng nhập đến `/api/login` trên Vercel qua HTTPS.
   Vercel đọc tab `Student`, tìm username/email, kiểm tra scrypt hash, role và
   trạng thái.
3. Nếu đăng nhập thành công, Vercel cấp JWT ký bằng `APP_SESSION_SECRET`, sống
   trong 4 giờ. Ứng dụng tự tạo/cập nhật hồ sơ SQLite cục bộ để gắn dữ liệu học
   tập với email và role trên Sheet, rồi giữ token trong SQLite để gọi API.
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
- Bản Windows đóng gói không bật tự đăng ký. Giáo viên/quản trị viên thêm
  tài khoản mới trực tiếp vào tab `Student`, gồm username, email, hash scrypt,
  role, lớp và trạng thái `active`.
- Vercel xác thực trực tiếp từ `Student`; lần đăng nhập thành công đầu tiên sẽ
  tự tạo hồ sơ cục bộ theo email, username, tên, lớp và role từ Sheet. Không
  cần tạo trước cùng một hồ sơ trong SQLite.
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
   repo).
4. Thư mục `vercel` chứa riêng API TypeScript, thư viện dùng chung và cấu hình
   Vercel. Nó không chứa `app.py` nên Vercel không thể khởi chạy ứng dụng
   desktop Python làm Function.
5. Trong **Settings → Build and Deployment**, đặt **Framework Preset** thành
   **Other** (không để Flask/Python), và xác nhận **Root Directory** là `vercel`.
   File `vercel/vercel.json` đã đặt `framework: null` và `buildCommand: null`
   để buộc Vercel dùng chế độ Other và không chạy custom build command.
   Cấu hình function áp dụng chung cho các file `api/*.ts`; vì vậy không cần
   khai báo đường dẫn riêng cho `api/transcribe.ts`.
   Không đặt `npm run typecheck` làm Build Command: lệnh đó chỉ kiểm tra kiểu,
   không tạo deployment output.
   Để Install Command mặc định (npm install) và Output Directory trống. Không
   cần `.vercelignore` trong `vercel/`, vì thư mục này đã là riêng biệt và
   không chứa ứng dụng desktop `app.py`.
6. **Xóa `.vercelignore` ở repository root** nếu repo còn file này. Bản cũ dùng
   allowlist `api/`, `lib/`, `package.json` ở root và sẽ loại các file thật nằm
   dưới `vercel/api/`; log kiểu `Found .vercelignore (repository root)` và
   `Removed ... ignored files` xác nhận file này đang ảnh hưởng deployment.
   Commit/push việc xóa file trước khi deploy lại.
7. Sau khi push thay đổi, tạo deployment mới từ commit đó. Nếu domain
   `cva-liard.vercel.app` là production domain, phải promote deployment mới lên
   **Production** (hoặc deploy nhánh Production Branch), không chỉ redeploy một
   Preview deployment. Build Logs phải cài dependency Node/npm và deployment
   phải liệt kê các Function dưới `api/`, trong đó có `api/login.ts`.

## C. Đặt Environment Variables

Tạo một tệp chỉ dành cho Vercel từ cấu hình local. Trước tiên, cập nhật key
Gemini/Groq trong `.env` nếu key cũ từng được chia sẻ; không tải nguyên `.env`
local lên Vercel vì tệp đó có thể chứa đường dẫn máy tính và biến chỉ dùng cho
desktop.

Chạy tại thư mục gốc repo bằng PowerShell:

```powershell
.\.venv\Scripts\python.exe tools\prepare_vercel_env.py
```

Nếu thiếu `GOOGLE_SHEET_ID` hoặc `GOOGLE_DRIVE_FOLDER_ID`, công cụ sẽ hỏi giá
trị tương ứng. Dùng ID từ URL (công cụ cũng chấp nhận URL Google Sheet/Drive).
Nó đọc service-account JSON qua `GOOGLE_SERVICE_ACCOUNT_PATH` trong `.env`,
hoặc hỏi đường dẫn nếu biến đó chưa có. Credentials được encode Base64 thành
một dòng để tránh lỗi xuống dòng; Base64 không phải mã hóa bảo mật, vì vậy hãy
bảo vệ `.env.vercel` như một secret. Giá trị không được in ra màn hình. Tệp
`.env.vercel` được loại khỏi Git theo `.gitignore`.

Trong Vercel project → **Settings → Environment Variables**, dùng chức năng
nhập hàng loạt để tải `.env.vercel` lên hoặc dán nội dung tệp. Chọn môi trường
**Production** cho các biến (thêm Preview chỉ khi thật sự cần dùng tài khoản/
dữ liệu thử):

| Tên biến | Nội dung |
|---|---|
| `APP_SESSION_SECRET` | Chuỗi ngẫu nhiên bí mật, tối thiểu 32 ký tự |
| `GEMINI_API_KEY` | Gemini API key |
| `GROQ_API_KEY` | Groq API key |
| `GROQ_EVAL_MODEL` | (Tùy chọn) Model Groq chấm điểm; mặc định `llama-3.3-70b-versatile` |
| `GOOGLE_SERVICE_ACCOUNT_JSON_B64` | JSON service account được encode Base64 |
| `GOOGLE_SHEET_ID` | ID Google Spreadsheet |
| `GOOGLE_DRIVE_FOLDER_ID` | ID thư mục Drive lưu bài nộp |

Kiểm tra tất cả biến được gán cho **Production**. Không tải file service
account JSON riêng lên GitHub. Chia sẻ Google Spreadsheet và thư mục Drive
cho `client_email` trong service-account JSON, cấp quyền Editor; Google Sheets
API và Google Drive API phải được bật trong Google Cloud.

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

1. Đảm bảo tài khoản có username, email, password hash, role và trạng thái
   `active` trong tab `Student`.
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
  Build and Deployment**, đặt **Root Directory** thành `vercel` và
  **Framework Preset** thành **Other** (không phải Flask/Python), lưu lại và
  redeploy từ commit mới nhất có thư mục `vercel/`. Build Logs phải cho thấy
  Node/npm, không được dò Flask entrypoint.
- **Build báo `No Flask entrypoint found`:** Root Directory có thể đã đúng,
  nhưng **Framework Preset** vẫn chọn Flask. Đổi sang **Other**, xóa mọi
  Python-specific Build/Install command override, để Output Directory trống,
  rồi tạo deployment mới. Khi deploy đúng, request POST thiếu dữ liệu tới
  `/api/login` trả lỗi JSON HTTP 400 thay vì `FUNCTION_INVOCATION_FAILED`.
- **`/api/login` trả HTTP 404 `NOT_FOUND`:** deployment/domain hiện tại không
  có Function route đó. Xác nhận build mới thành công và Build Logs liệt kê
  `api/login.ts` như Function. Nếu Build Logs cảnh báo
  `Found .vercelignore (repository root)` cùng `Removed ... ignored files`,
  mở repo GitHub và xóa `.vercelignore` ở cùng cấp với `app.py`, commit/push
  thay đổi đó, rồi tạo deployment mới. Không thay bằng allowlist root cũ:
  Vercel cần đọc `vercel/api/login.ts` khi Root Directory là `vercel`.
  Nếu vẫn cảnh báo
  `Build output contains no "functions", "static", or "services" directory`,
  đẩy phiên bản mới có `buildCommand: null` trong `vercel/vercel.json` lên repo
  rồi tạo deployment mới. Không chạy `npm run build` hoặc `npm run typecheck`
  làm Build Command: các lệnh này không tạo deployment output. Vercel cần tự
  phát hiện/đóng gói các file trong `api/`.
  Kiểm tra URL deployment trực tiếp của build mới;
  nếu URL đó chạy nhưng `cva-liard.vercel.app` vẫn 404, gán/promote deployment
  thành **Production** để domain production trỏ đúng deployment. Không cần
  `.vercelignore` trong thư mục `vercel/`.
- **Build báo `[unused_function]` cho `api/transcribe.ts`:** cấu hình cũ có
  override riêng cho function này nhưng Vercel không nhận diện đường dẫn đó
  trong deployment. File `vercel/vercel.json` hiện chỉ dùng pattern chung
  `api/*.ts`, đặt thời hạn tối đa 120 giây cho các function và bỏ pattern riêng.
  Push config mới và redeploy; nếu function vẫn không xuất hiện trong tab
  Functions thì kiểm tra commit có chứa `vercel/api/transcribe.ts`.
- **Đăng nhập bị từ chối:** kiểm tra username/email, scrypt hash trong cột
  `password` hoặc `password_hash`, role và trạng thái `active` trong tab
  `Student`. Sau khi deploy bản API có chẩn đoán an toàn, Vercel Runtime Logs
  có thể ghi một trong các mã `account_not_found`, `account_role_mismatch`,
  `account_email_invalid`, `account_inactive` hoặc
  `password_mismatch_or_hash_invalid`. Các mã này không chứa thông tin tài khoản
  hay mật khẩu; dùng chúng để xác định trường nào cần sửa, không chia sẻ secrets.
- **API báo thiếu/cấu hình sai Google service account hoặc
  `GOOGLE_SHEET_ID`:** xác nhận `.env.vercel` đã được import vào đúng Vercel
  project và môi trường **Production**, rồi redeploy. Tệp có thể dùng
  `GOOGLE_SERVICE_ACCOUNT_JSON_B64`; API vẫn hỗ trợ biến JSON cũ
  `GOOGLE_SERVICE_ACCOUNT_JSON` để tương thích. Service-account JSON phải có
  `client_email`, `private_key`. Không gửi giá trị biến qua chat.
- **Vercel trả `The application session signing secret is not configured`:**
  thêm `APP_SESSION_SECRET` Production, dài tối thiểu 32 ký tự, rồi redeploy.
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
