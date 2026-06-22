# WB Marks App

Server-side v1 for:

- loading a WB draft supply by `preorderID`,
- resolving GTIN mapping,
- issuing Honest Signs through `label.teksher.kg` API,
- waiting for marking completion,
- saving CSV artifacts,
- creating `transgran` operations,
- storing runs, operations, marks, and artifacts in PostgreSQL-compatible storage.

## Architecture

- `FastAPI` backend with server-rendered web GUI
- `SQLAlchemy` persistence layer
- `Chrome` extension that injects the `ВыпуститьЧЗ` button into `seller.wildberries.ru`
- `WB API` for draft retrieval
- `Teksher API` for auth, emission, marking, CSV, and transgran

`wbarcode.ru` is intentionally not included in v1.

## Run locally

```powershell
python -m pip install -e .
$env:DATABASE_URL = "sqlite:///./wb_marks_app.db"
$env:APP_BASE_URL = "http://localhost:8000"
python -m wb_marks_app
```

Open:

- [http://localhost:8000/settings](http://localhost:8000/settings)
- [http://localhost:8000/runs](http://localhost:8000/runs)
- [http://localhost:8000/labels](http://localhost:8000/labels)

## Label printing

The web UI can print 60x40 mm labels from:

- saved WB workflow runs: open a run and click `Печать этикеток`;
- manual input: open `/labels`, paste Honest Sign GS1/DataMatrix payloads, fill WB fields, and print the preview.

The label renderer preserves the GS separator (`ASCII 29`, `\x1d`) inside ЧЗ codes and renders:

- WB barcode as Code128;
- ЧЗ payload as Data Matrix with an initial FNC1 marker.

## Docker

```powershell
docker build -t wb-marks-app .
docker run --rm -p 8000:8000 `
  -e DATABASE_URL="postgresql+psycopg://user:pass@host:5432/dbname" `
  -e APP_BASE_URL="http://your-vps:8000" `
  wb-marks-app
```

## Settings model

The app stores operational settings in the database:

- `wb_api_token`
- `wb_api_base_url`
- `teksher_username`
- `teksher_password`
- `teksher_transgran_recipient_name`
- `teksher_transgran_recipient_inn`
- `teksher_transgran_recipient_kpp`
- `mapping_mode`
- `mapping_payload`
- `artifact_storage_dir`
- `transgran_document_number_prefix`
- `step_timeout_seconds`

### Mapping JSON

The settings page accepts JSON with any of these keys:

```json
{
  "size_to_gtin": {
    "S": "04700063257354",
    "M": "04700063257361"
  },
  "vendor_size_to_gtin": {
    "nbf_black_z:S": "04700063257354"
  },
  "barcode_to_gtin": {
    "2043467498261": "04700063257354"
  }
}
```

Resolution order:

1. `barcode_to_gtin`
2. `vendor_size_to_gtin`
3. `size_to_gtin`

## REST API

- `POST /api/launches`
- `GET /api/launches/{id}`
- `GET /api/launches/{id}/items`
- `GET /api/settings`
- `PUT /api/settings`
- `POST /api/settings/validate`
- `GET /api/artifacts/{artifact_id}`
- `GET /health`
- `GET /ready`

Launch payload:

```json
{
  "draft_id": "50680482",
  "source_url": "https://seller.wildberries.ru/..."
}
```

## Chrome extension

The extension files live in [C:\Users\1\Documents\chrome\extension](C:\Users\1\Documents\chrome\extension).

Load it as an unpacked extension in Chrome:

1. Open `chrome://extensions`
2. Enable developer mode
3. Click `Load unpacked`
4. Choose `C:\Users\1\Documents\chrome\extension`
5. Open extension options and set the backend URL

The content script injects the `ВыпуститьЧЗ` button on WB pages, tries to detect the current draft id, then asks the backend to create or resume the corresponding run.

## Tests

```powershell
$env:PYTHONPATH = "C:\Users\1\Documents\chrome\src"
python -m unittest discover -s tests -v
```
