# DNS A Record Updater

Small Python script to update a cPanel DNS A record to your current public IPv4.

## What it does

- Detects your current public IPv4 address
- Finds the A record in your cPanel zone
- Updates that record through cPanel API (UAPI, with API2 fallback)
- Writes success/error logs to `update_a_record.log`

## Requirements

- Python 3.10+
- cPanel API token for the account that owns the zone

Install dependencies:

```bash
pip install -r requirements.txt
```

## Configuration with .env

Create a `.env` file in the same directory as the script:

```env
CPANEL_HOST=cp.example.com
CPANEL_USER=cpaneluser
CPANEL_TOKEN=your_api_token
CPANEL_DOMAIN=example.com
CPANEL_RECORD_NAME=app.example.com

# Optional aliases supported by the script:
# CPANEL_NAME=app.example.com
```

Required values are:

- `CPANEL_HOST`
- `CPANEL_USER`
- `CPANEL_TOKEN`
- `CPANEL_DOMAIN`
- `CPANEL_RECORD_NAME` (or `CPANEL_NAME`)

## Usage

Run with values from `.env`:

```bash
python update_a_record.py
```

Or override any setting via CLI:

```bash
python update_a_record.py \
  --host cp.example.com \
  --user cpaneluser \
  --token your_api_token \
  --domain example.com \
  --name app.example.com
```

Useful options:

- `--dry-run` show what would change without updating DNS
- `--ttl 300` set TTL during update
- `--line 123` target a specific record line (useful if duplicates exist)
- `--timeout 30` HTTP timeout in seconds
- `--insecure` disable TLS verification (not recommended)

## Logs

The script writes to:

- `update_a_record.log`

## Exit codes

- `0` success
- `1` request/API/validation failure
- `2` HTTP error response
