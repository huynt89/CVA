# Checklist chuẩn bị repo và phát hành CVA

Hướng dẫn này bao gồm repo private, Vercel, Google Sheet `Student` và phát hành
ứng dụng Windows. Các thao tác tài khoản GitHub/Vercel/Google do bạn tự thực
hiện.

## 1. Chuẩn bị repo private `CVA`

1. Clone repo `CVA` về máy.
2. Sao chép mã nguồn vào thư mục gốc repo, giữ nguyên các thư mục:
   `.github`, `docs`, `static`, `templates`, `tests`, `tools`, `vercel`
   cùng các file gốc như `app.py`, `launcher.py`, `launcher_config.py`,
   `requirements.txt`, `.gitignore` và `README.md`.
3. **Không sao chép/commit** `.env`, `.session-secret`, `*.db`, `uploads`,
   `huyntupload-drive-app-778fe4e22bff.json` hoặc JSON service account khác,
   `.venv`, `node_modules`, `build`, `dist` hay dữ liệu cá nhân.
4. Kiểm tra danh sách sẽ commit:

   ```powershell
   git status --short
   git add .
   git status
   ```

   Nếu thấy credential, database hoặc file cá nhân đã được stage, bỏ stage file
   đó trước khi commit. Nếu credential từng bị commit/chia sẻ, hãy thu hồi và
   tạo mới; xóa ở commit sau không xóa khỏi lịch sử Git.
5. Commit và push mã nguồn lên repo private.

## 2. Chuẩn bị tài khoản trong tab `Users`

1. Đổi tên tab tài khoản hiện có từ `Student` thành `Users`, giữ nguyên dữ liệu
   và các dòng tài khoản hiện có.
2. Bảo đảm header có các cột `full_name`, `username`, `email`, `password`,
   `role`, `class_name`, `status`. Cột `password` đã được bạn thêm sau
   `username`.

   Mỗi tài khoản học sinh và giáo viên cần có một dòng. `role` là `student`
   hoặc `teacher`; `status` là `active` hoặc `locked`. Sau khi xác thực từ Sheet,
   ứng dụng tự tạo hồ sơ SQLite cục bộ nếu chưa có.
3. Với từng tài khoản, tạo hash trên máy tin cậy:

   ```powershell
   py tools\generate_password_hash.py
   ```

   Nhập mật khẩu hai lần; mật khẩu không hiển thị trên màn hình. Dán hash được
   in vào cột `password` cùng dòng tài khoản. Không lưu mật khẩu dạng rõ trong
   Sheet.
4. Đồng bộ Student sẽ giữ nguyên cột hash và các dòng role `teacher`; không xóa
   các tài khoản giáo viên bạn đã thêm.
5. Chia sẻ Google Sheet cho `client_email` của service account; cấp quyền Editor
   để app có thể đọc/ghi các sheet. Chia sẻ cả thư mục Drive đích cho account đó.

## 3. Tạo Vercel project và lưu secrets

1. Đăng nhập [Vercel](https://vercel.com), chọn **Add New → Project**, kết nối
   GitHub và import repo private `CVA`.
2. Chọn `vercel` làm **Root Directory** và **Other** làm Framework Preset.
   Không chọn thư mục gốc repo vì ở đó có `app.py` của ứng dụng desktop.
   Nếu project trước đây dùng Flask/Python, phải đổi preset và xóa các lệnh
   Build/Install riêng cho Python; để Output Directory trống. Không cần
   `.vercelignore` bên trong `vercel/`. Xóa `.vercelignore` cũ ở repository
   root nếu còn, vì allowlist cũ có thể loại `vercel/api/` khỏi deployment.
3. Sau khi deploy, xác nhận `api/login.ts` xuất hiện trong danh sách Functions.
   Đảm bảo deployment mới được đặt Production nếu ứng dụng gọi production
   domain; Preview deployment không tự chuyển production alias.
4. Tạo tệp import dành riêng cho Vercel. Trước đó, cập nhật các API key đã từng
   bị chia sẻ và xác nhận `.env` có `GOOGLE_SERVICE_ACCOUNT_PATH` trỏ đến JSON
   service account:

   ```powershell
   .\.venv\Scripts\python.exe tools\prepare_vercel_env.py
   ```

   Nếu `.env` chưa có `GOOGLE_SHEET_ID` hoặc `GOOGLE_DRIVE_FOLDER_ID`, nhập ID
   tương ứng khi được hỏi. Script tạo `.env.vercel` gồm các biến server cần
   thiết và `GOOGLE_SERVICE_ACCOUNT_JSON_B64` (Base64 không phải mã hóa bảo
   mật); không đưa đường dẫn máy tính vào file. Không mở/dán nội dung file vào
   chat.
5. Mở **Settings → Environment Variables** của đúng Vercel project, dùng chức
   năng import để tải `.env.vercel` lên và gán biến cho **Production**.
   Đặt biến là **Sensitive/Secret** nếu Vercel hiển thị lựa chọn đó. Không đưa
   chúng vào GitHub Actions, `static/js/cloud-config.json`, EXE, chat hoặc log.
6. Sau khi lưu biến, redeploy Production để Function nhận cấu hình mới.
7. Mở **Settings → Domains** để lấy URL production,
   ví dụ `https://cva-api-yourname.vercel.app`.

## 4. Cấu hình URL Vercel trong ứng dụng

Mở `static/js/cloud-config.json` và thay placeholder:

```json
{
  "vercel_api_base_url": "https://<domain-production>.vercel.app"
}
```

Đây là URL công khai, không phải secret. Commit/push file này; nếu đổi URL sau
khi phát hành thì cần build và phát hành lại ứng dụng desktop.

## 5. Phát hành ứng dụng Windows

Để build một EXE độc lập trên Windows (không kèm launcher hoặc ZIP), chạy:

```powershell
.\.venv\Scripts\python.exe tools\build_windows.py
```

File duy nhất được tạo trong thư mục đầu ra mới `dist\cva-distribution` là
`CVA Let's speak!.exe`. EXE không hiện terminal, mở giao diện trong cửa sổ
Edge/Chrome kiểu ứng dụng và dừng server cục bộ khi cửa sổ đó đóng. Icon lấy từ
`static/images/cva-logo.ico`; máy chạy cần cài Edge hoặc Chrome.

Quy trình GitHub Actions bên dưới vẫn là quy trình phát hành cũ có launcher và
ZIP để hỗ trợ cập nhật. Dùng build độc lập ở trên nếu cần đúng một file EXE.

Trong repo private GitHub, vào **Settings → Secrets and variables → Actions**:

- Repository variables:
  - `DISTRIBUTION_REPOSITORY`: `owner/repo-public`
  - `UPDATE_PUBLIC_KEY_B64`: public key update Ed25519
- Repository secrets:
  - `CVA_UPDATE_PRIVATE_KEY_B64`: private key update Ed25519
  - `DISTRIBUTION_TOKEN`: token có quyền `Contents: read and write` trên repo
    phân phối public

Tạo cặp khóa trong repo:

```powershell
.\.venv\Scripts\python.exe tools\release_update.py generate-key
```

Không commit private signing key. Kiểm tra `cloud-config.json` không còn
`REPLACE_`, sau đó tạo/push tag:

```powershell
git tag v1.0.0
git push origin v1.0.0
```

Kiểm tra workflow trong tab **Actions** của repo private và **Releases** trong
repo public. Bản phát hành gồm `CVA-Launcher.exe`, ZIP ứng dụng,
`update-manifest.json` và `update-manifest.sig`.

## 6. Kiểm tra trước khi phân phối

1. Đăng nhập trên ứng dụng Windows bằng một tài khoản giáo viên và một tài
   khoản học sinh có trong tab `Users`.
2. Kiểm tra giáo viên đọc/ghi `Users`, `Lesson`, `Practice`; xác nhận học
   sinh không thể gọi thao tác dành riêng giáo viên.
3. Thử gửi Practice, tải audio/video nhỏ lên Drive, xác nhận link được trả về
   và transcript được tạo qua Groq.
4. Kiểm tra Vercel Logs không ghi API key, mật khẩu, JWT hoặc private key.
5. Chỉ phân phối khi đăng nhập, Google Sheets/Drive và AI đều hoạt động.

## Lưu ý bảo mật và hạn mức

- Mật khẩu trong `Users` là scrypt hash; đổi mật khẩu bằng cách tạo hash mới và
  thay ô `password_hash`. Đặt `is_active` thành `FALSE` để khóa đăng nhập.
- JWT phiên làm việc được ký bằng `APP_SESSION_SECRET`, hết hạn sau 4 giờ. Nếu
  đổi secret, mọi phiên cũ sẽ hết hiệu lực.
- Giới hạn lượt gọi hiện lưu trong bộ nhớ của Vercel Function, nên chỉ là giới
  hạn best-effort; nó không phải bộ đếm bền vững giữa các instance/deployment.
  Theo dõi usage ở Vercel, Google, Gemini và Groq; đặt cảnh báo/hạn mức ở các
  dịch vụ cung cấp khi có thể.
- Bản miễn phí không đồng nghĩa mọi dịch vụ Google/Gemini/Groq đều miễn phí.
  Kiểm tra quota/điều khoản hiện hành trước khi đưa vào sử dụng rộng rãi.
- Secret ở các dịch vụ trước đây không tự bị xóa khi thay đổi code. Chỉ thu hồi
  các credential cũ sau khi đã xác nhận hệ thống mới chạy tốt.
