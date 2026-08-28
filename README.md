# VTC Medical Vocabulary

Mobile-first vocabulary and spelling practice for the English-taught VTC medical and rehabilitation programme.

This repository contains the application code and deployment scaffolding adapted from the earlier IELTS vocabulary webapp. Private VTC course materials, personal study records, vocabulary datasets, API keys, OAuth credentials, and sync tokens are intentionally excluded.

## Security boundary

- Keep API keys, OAuth secrets, and `APP_SECRET_KEY` in the hosting provider's secret manager.
- Never commit `.env` files, credential JSON, refresh tokens, progress exports, or private course material.
- Configure `SYNC_ALLOWED_ORIGINS` with exact HTTPS origins before enabling cross-origin sync.
- Public Google Drive is reserved for explicitly public-safe worklist or vocabulary assets; user progress and authentication data must remain private.
- Verify licensing before publishing any vocabulary dataset derived from a commercial IELTS source.

## Local development

The source tree intentionally has no bundled vocabulary dataset. Supply a permitted local worklist or generated data file outside Git, configure the required environment variables, then run:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
set -a
source .env
set +a
python3 main.py
```

Open http://localhost:8080. API and sync behavior must be tested against the intended deployment before publication.

## Status

The project is in migration and security-review phase. GitHub hosting, API selection, public worklist upload, and cross-device synchronization are not yet approved as production behavior.
