# Entra to IBM Security Verify Sync

Sync Azure AD (Entra ID) users into IBM Security Verify over SCIM. Run as a CLI or in a container (GKE/cron friendly).

## Requirements
- Python 3.12+
- Azure AD app registration with client credentials (Graph scope `https://graph.microsoft.com/.default`)
- IBM Security Verify SCIM Users endpoint URL and bearer token

## Configuration
1. Copy `.env.example` to `.env` and fill secrets:
   - `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET`
   - `VERIFY_BASE_URL` (e.g., `https://yourtenant.verify.ibm.com/v2.0/scim/v2`)
   - `VERIFY_API_TOKEN`
2. Adjust `config/mapping.example.yaml` as needed. It controls attribute mapping and sync options (e.g., deactivating disabled accounts).

## Install & Run (CLI)
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\Activate.ps1
pip install .
entra-sync sync --config config/mapping.example.yaml --env-file .env
entra-sync repl --config config/mapping.example.yaml --env-file .env
# Non-interactive
entra-sync whatif --config config/mapping.example.yaml --env-file .env --output plan.json --format json
entra-sync compare user@contoso.com --config config/mapping.example.yaml --env-file .env --output compare.csv --format csv
```
Use `--dry-run` to preview without making changes, and `-v` for debug logs.

## Docker
Build and run locally:
```bash
docker build -t entra-to-isv:local .
docker run --rm --env-file .env -v $(pwd)/config:/app/config:ro entra-to-isv:local sync --config /app/config/mapping.example.yaml
```

### GKE / CronJob notes
- Use the sample manifest in `k8s/cronjob.yaml` and update the image and schedule.
- Create a ConfigMap named `entra-sync-mapping` with `mapping.yaml` (e.g., `kubectl create configmap entra-sync-mapping --from-file=config/mapping.example.yaml=mapping.yaml`).
- Create a Secret named `entra-sync-env` containing `.env` (e.g., `kubectl create secret generic entra-sync-env --from-file=.env`). If you sync from GCP Secret Manager, point the CSI driver or sync tool to produce the same Secret/key.
- The CronJob mounts `/app/config/mapping.yaml` and `/var/secrets/.env`, and runs `entra-sync sync --config /app/config/mapping.yaml --env-file /var/secrets/.env`.

## Behavior
- Reads users from Microsoft Graph with selected fields.
- Maps attributes per YAML; defaults cover `userName`, names, `displayName`, `mail` → emails, and `accountEnabled` → `active`.
- Creates missing users in Verify; updates existing users when mapped fields change.
- If `deactivate_disabled` is true, disabled Azure users are marked inactive in Verify (no hard deletes; deletion is CLI-only and not implemented by default).

## REPL
- `Synchronize`: Runs the full sync now.
- `Compare`: Enter a UPN to compare Azure vs Verify and see diffs.
- `What-if`: Plans the sync and lists would-create and estimated updates.

## Non-interactive commands
- `whatif`: Outputs a full plan. Use `--format json|csv` and `--output path` to export.
- `compare <upn>`: Compares a single user. Use `--format json|csv` and `--output path`.

## Extend
- Adjust mapping to include phone numbers by toggling `include_mobile_phone` in the sync section.
- Add custom attribute mappings under `attribute_mapping` (e.g., `name.middleName: middleName`).

## Testing
```bash
pip install .[dev]
pytest
```
