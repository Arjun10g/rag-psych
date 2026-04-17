#!/usr/bin/env bash
# Seed a remote Postgres (Neon, Fly Postgres, RDS, ...) with the public
# corpus by running ingest from your laptop against $DATABASE_URL.
#
# Usage:
#   DATABASE_URL='postgresql://user:pass@host/db' ./scripts/seed_remote.sh
#
# Optional:
#   SOURCES="mtsamples pubmed icd11"   # default; pass space-separated
#
# Notes:
#   - This script intentionally REJECTS any --sources entry of `dsm5`.
#     The DSM PDF is local-personal-use only and must never be ingested
#     into a remote DB. See ingest/sources/dsm.py.
#   - Re-runs are mostly cache hits (data/cache/{pubmed,icd11}/), so
#     subsequent seeds are minutes, not the full 15+ of a fresh fetch.

set -euo pipefail

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "error: DATABASE_URL is required" >&2
  echo "  example:  DATABASE_URL='postgresql://user:pass@ep-xyz.neon.tech/rag' \\" >&2
  echo "            $0" >&2
  exit 1
fi

SOURCES="${SOURCES:-mtsamples pubmed icd11}"

if echo "$SOURCES" | grep -qw "dsm5"; then
  echo "error: refusing to ingest dsm5 against a remote DB" >&2
  echo "  DSM-5 is licensed for local personal use only." >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "error: .venv not found. Run: python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

echo "── Seeding remote DB ──"
echo "  target: $(echo "$DATABASE_URL" | sed -E 's|://[^:]+:[^@]+@|://***:***@|')"
echo "  sources: $SOURCES"
echo ""

# shellcheck disable=SC2086
DATABASE_URL="$DATABASE_URL" .venv/bin/python ingest/run.py --sources $SOURCES
