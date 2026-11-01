# CVA Let's Speak!

Ứng dụng desktop Flask hỗ trợ học tiếng Anh. Mã nguồn lưu trong repo private
`CVA`; GitHub Actions đóng gói ứng dụng Windows và đẩy bản phát hành sang repo
public. Vercel chạy API phía server. Tài khoản được xác thực bằng thông tin trong tab
`Users`, nơi lưu cả tài khoản học sinh và giáo viên.

Không commit `.env`, SQLite database, uploads, Google service-account JSON hoặc
API key. Không đặt Gemini/Groq/Google private credentials trong EXE, mã nguồn,
GitHub Actions variables hay file cấu hình client.

## Chạy ứng dụng trên máy phát triển

Trên Windows, mở PowerShell tại thư mục dự án:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

`.env` chỉ dành cho phát triển cục bộ. `static/js/cloud-config.json` chỉ chứa
URL Vercel công khai, không chứa API key.

## Build ứng dụng Windows thành một file

Trên Windows, cài dependencies trong `.venv`, sau đó chạy:

```powershell
.\.venv\Scripts\python.exe tools\build_windows.py
```

File đầu ra là `dist\cva-distribution\CVA Let's speak!.exe`. Bản này không mở
terminal; nó mở giao diện trong cửa sổ ứng dụng Edge/Chrome và dừng web server
cục bộ khi cửa sổ ứng dụng đóng. Edge hoặc Chrome phải được cài trên máy.
Icon Windows lấy từ `static/images/cva-logo.ico`.

## Backend, đăng nhập và dữ liệu

- **Vercel Functions** xử lý xác thực, Gemini/Groq, Google Sheets và Drive.
  API key, Google service account, Sheet ID và Drive folder ID chỉ lưu trong
  Environment Variables của Vercel.
- **Google Sheet `Users`** giữ tài khoản học sinh và giáo viên. Cần có cột
  `username`, `email`, `password`, `role`, `status`; cột `password` lưu scrypt
  hash, không lưu mật khẩu dạng rõ. Chạy `tools/generate_password_hash.py` để
  tạo hash scrypt. Bản Windows đóng gói không tự đăng ký tài khoản; giáo viên
  thêm tài khoản vào Sheet `Users`, sau đó người dùng đăng nhập bằng tài khoản đó.
- **Google Sheet `HeadTeacher`** giữ tài khoản headteacher với các cột
  `full_name`, `username`, `password`, `email`, `role`, `status`; `role` phải là
  `headteacher`, `status` là `active`, và `password` lưu scrypt hash. Tài khoản
  đăng nhập trong mục Teacher và mới có quyền quản lý các tài khoản giáo viên.
- **Ứng dụng Windows** giữ dữ liệu nghiệp vụ cục bộ trong SQLite. Khi đăng nhập,
  Vercel tra `Users` hoặc `HeadTeacher` theo mục đăng nhập đã chọn.
  Ứng dụng tự tạo/cập nhật hồ sơ SQLite cục bộ từ tài khoản đã xác thực để lưu
  dữ liệu học tập; thông tin đăng nhập được quyết định bởi Sheet. Đồng bộ danh
  sách hỗ trợ cả hai role và giữ mật khẩu hash. Thay đổi hồ sơ giáo viên trong
  My Account được ghi ngược vào đúng tab của tài khoản (`Users` hoặc `HeadTeacher`).
- **Media** được tải trực tiếp từ Flask lên phiên tải lên resumable của Google
  Drive do Vercel cấp. Vercel chỉ lưu secret, xác nhận chủ sở hữu file, đặt
  quyền xem cho liên kết Drive và xử lý transcription.

Xem [hướng dẫn Vercel và Google Sheet](docs/VERCEL_SETUP_VI.md) để cấu hình
`Users`, API secrets và deploy. Xem [checklist phát hành](docs/RELEASE_SETUP_CHECKLIST.md)
cho quy trình repo private → repo public → ứng dụng Windows.

## Repo mã nguồn private và repo phân phối public

Giữ mã nguồn trong repo private `CVA`. Repo public chỉ dùng để chứa GitHub
Releases với:

- `CVA-Launcher.exe`
- `CVA-EngClassAI-<tag>.zip`
- `update-manifest.json`
- `update-manifest.sig`

Workflow [`.github/workflows/release-windows.yml`](.github/workflows/release-windows.yml)
build ứng dụng Windows, ký update manifest và xuất bản các file khi push tag
bắt đầu bằng `v`.

### Cấu hình ký bản cập nhật trên GitHub

1. Tạo cặp khóa Ed25519:

   ```powershell
   .\.venv\Scripts\python.exe tools\release_update.py generate-key
   ```

2. Trong repo private, tại **Settings → Secrets and variables → Actions**,
   thêm variables:
   - `DISTRIBUTION_REPOSITORY`: `owner/repo-public`
   - `UPDATE_PUBLIC_KEY_B64`: public key vừa tạo
3. Thêm secrets:
   - `CVA_UPDATE_PRIVATE_KEY_B64`: private key vừa tạo
   - `DISTRIBUTION_TOKEN`: fine-grained GitHub token có quyền **Contents: read
     and write** trên repo public
4. Không commit private signing key. API keys ứng dụng không đặt ở GitHub; hãy
   cấu hình chúng trong Vercel.
5. Hoàn tất `static/js/cloud-config.json`, sau đó tạo tag, ví dụ:

   ```powershell
   git tag v1.0.0
   git push origin v1.0.0
   ```

Launcher xác minh chữ ký Ed25519 và SHA-256 trước khi cài. Mỗi phiên bản nằm
trong thư mục riêng tại `%LOCALAPPDATA%\CVA\EngClassAI\versions`; dữ liệu người
dùng nằm ngoài các thư mục phiên bản tại `%LOCALAPPDATA%\CVA\EngClassAI\data\`.
Ứng dụng phát hành hiện gồm launcher EXE và ZIP, không phải một EXE duy nhất.

## Kiểm tra

Chạy Python tests:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Kiểm tra Vercel API:

```powershell
Push-Location .\vercel
npm ci
npm run typecheck
Pop-Location
```
