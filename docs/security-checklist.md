# Security Checklist

Run through this before **every** commit and before sharing the repo or demo.

## Secrets hygiene

- [ ] `.env` exists locally, is NOT tracked by git
  - Verify: `git ls-files | grep -E "^\.env$"` returns nothing
- [ ] `.env.example` has only placeholders, no real values
  - Verify: `grep -E "sk-ant-[A-Za-z0-9]" .env.example` returns nothing
- [ ] No hardcoded API keys in any Python file
  - Verify: `grep -rEn "sk-ant-[A-Za-z0-9_-]{10,}" --include="*.py" .` returns nothing
- [ ] No API keys in any Markdown file
  - Verify: `grep -rEn "sk-ant-[A-Za-z0-9_-]{10,}" --include="*.md" .` returns nothing
- [ ] `ANTHROPIC_API_KEY` is loaded from `os.environ`, never a literal default
- [ ] Spend limit set at console.anthropic.com → Settings → Limits
- [ ] No hardcoded Postgres password in `docker-compose.yml`
  - Verify: `grep -E 'POSTGRES_PASSWORD:\s*[^$ ]' docker-compose.yml` returns nothing
  - (A passing value must be `${POSTGRES_PASSWORD}`, i.e. starts with `$`)
- [ ] `POSTGRES_PASSWORD` in `.env` is NOT the placeholder from `.env.example`
  - Verify: `grep "REPLACE_ME" .env` returns nothing

## Data protection

- [ ] `data/mtsamples.csv` is not tracked
  - Verify: `git ls-files data/ | grep -v .gitkeep` returns nothing
- [ ] No `.csv`, `.parquet`, or `.jsonl` files are tracked (except eval fixtures)
- [ ] No synthetic "realistic PHI" in any fixture — fake names must be obviously fake
      ("John Doe", "Jane Smith"), fake dates must be clearly fake ("1900-01-01")
- [ ] Audit logs contain query **hashes**, not raw query text
- [ ] Chunk text is not logged at INFO level in production mode

## Input validation

- [ ] Pydantic model on `/query` enforces `max_length=2000` on the query string
- [ ] `k` parameter is bounded: `ge=1, le=20`
- [ ] Oversized queries return HTTP 400 with a generic message (no internals)
- [ ] All SQL uses parameter binding via psycopg — search for `f"...{...}..."`
      anywhere near `execute(` calls and verify none exist
  - Verify: `grep -rEn 'execute.*f"' --include="*.py" .` returns nothing

## Container hardening

- [ ] Every Dockerfile has `USER <nonroot>` before application CMD
- [ ] No `--privileged` in docker-compose.yml
- [ ] Environment variables passed via `env_file` or `environment`, never baked
      into the image at build time
- [ ] `.dockerignore` excludes `.env`, `data/`, `.git/`, and `docs/`

## Network posture

- [ ] CORS in FastAPI allows a single same-origin value (default
      `http://localhost:8000` since the HTMX UI is served by FastAPI itself),
      not `"*"`. Configurable via the `CORS_ORIGIN` env var.
- [ ] `/health` endpoint does NOT reveal: stack traces, package versions,
      database schema, internal hostnames, config values
- [ ] Rate limiter in place on `/query` (default 30/min per IP is fine for demo)

## Git history

- [ ] No `.env` has ever been committed
  - Verify: `git log --all --full-history --source -- .env` returns nothing
- [ ] No API key has ever been committed
  - Verify: `git log --all -p | grep -E "sk-ant-[A-Za-z0-9_-]{10,}"` returns nothing
- [ ] If ANY secret has ever touched git history, rotate the key immediately
      and rewrite history with `git filter-repo` — a `.gitignore` after the fact
      does not undo the exposure

## Pre-commit quick check (copy-paste)

```bash
# Run from repo root before every commit
set -e
echo "── Staged files ──"
git status --short
echo ""
echo "── Secret scan ──"
if grep -rEn "sk-ant-[A-Za-z0-9_-]{10,}" --include="*.py" --include="*.md" --include="*.yml" --include="*.yaml" . 2>/dev/null; then
  echo "❌ Possible API key found in tracked files"; exit 1
fi
if git diff --cached --name-only | grep -E "^\.env$|\.csv$|\.parquet$"; then
  echo "❌ Refusing to commit .env or data files"; exit 1
fi
echo "✓ Clean"
```

Consider adding this as a pre-commit hook in `.git/hooks/pre-commit`.

## If a secret leaks

1. **Rotate the key immediately** at console.anthropic.com — do not wait.
2. Revoke the old key.
3. Check your Anthropic usage dashboard for anomalous activity.
4. Rewrite git history with `git filter-repo` or BFG to remove the key.
5. Force-push the cleaned history (only safe on a solo repo; coordinate on shared ones).
6. Review how the leak happened and add a pre-commit hook or secret scanner
   (gitleaks, trufflehog) to prevent recurrence.
