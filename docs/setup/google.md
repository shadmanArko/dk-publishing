# Google: the Sheet and the media folder

You need three things in `dk.json` → `google`: `service_account_file`, `sheet_id`, `drive_folder_id`.

## 1. Create a service account and key
1. Open [console.cloud.google.com](https://console.cloud.google.com), create a project (or pick one).
2. **APIs & Services → Library**: enable **Google Sheets API** and **Google Drive API**.
3. **IAM & Admin → Service Accounts → Create service account** (any name). Skip the optional roles.
4. Open it → **Keys → Add key → Create new key → JSON**. A file downloads.
5. Save it as `google-key.json` in the same folder as `dk.json` and run `chmod 600 google-key.json`.
6. Copy the account's email (looks like `name@project.iam.gserviceaccount.com`).

## 2. The Sheet and the Drive folder
1. Create an empty Google Sheet. Copy its ID from the URL: `docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`.
   Put it in `google.sheet_id`.
2. **Share** the Sheet with the service account email as **Editor**.
3. Create a Drive folder for media. Copy its ID from the URL: `drive.google.com/drive/folders/`**`THIS_PART`**.
   Put it in `google.drive_folder_id`.
4. **Share** the folder with the same email as **Viewer** (Editor also works).

## 3. Check
```bash
make check-setup      # the google line must say [ok]
make sheet-init       # builds the tabs, dropdowns and instructions in the Sheet
```

If it fails, the message names what is missing (usually a share that was not given to the email).
