# CVA Let's speak!

The project root is the source tree intended for private Repo1. Do not publish
local `.env` files, service-account credentials, SQLite databases, or uploads.
The root `.gitignore` excludes these items.

## Run locally on Windows

From the project directory, create the virtual environment and install the
project dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

Using the virtual environment's Python directly avoids relying on the global
Python installation or PowerShell script-activation policy.

## Firebase secrets

Firebase Functions source and deployment notes belong under `firebase/`.
Provider keys and Google service-account JSON are Secret Manager values, not
files in either repository. See [Firebase setup](firebase/README.md).

Firebase is not provisioned yet, so the existing AI and Google integrations
have not yet been moved behind authenticated Cloud Functions. Do not publish
the application until that migration is completed and deployed.

## Google Sheets sync

Configure `GOOGLE_SHEET_ID` (or `GOOGLE_SHEET_NAME`) and a valid service
account. The spreadsheet must contain worksheets named exactly `Student`,
`Lesson`, and `Practice`. Use the sync action on the matching teacher page to
import Student/Lesson rows or export Practice submissions. Teacher list pages
read from the local database and do not wait for Google Sheets; run a manual
sync when you need to refresh data from the spreadsheet.
