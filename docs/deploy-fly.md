# Deploying rag-psych to Fly.io

Architecture: **Fly.io for the API container, Neon for Postgres + pgvector.**
Both have free tiers that cover the demo. The API auto-stops when idle so
the bill stays at $0 between visits.

> **Critical:** never run `--sources dsm5` against the remote DB. The DSM
> chunks are licensed for local personal use only and must stay in your
> laptop's `pgdata` volume. The `scripts/seed_remote.sh` helper rejects
> any attempt to ingest DSM remotely.

---

## 1. Provision Postgres on Neon (free)

1. Sign up at [console.neon.tech](https://console.neon.tech) (GitHub auth, free).
2. Create a new project. Region: pick one near your Fly region (we'll use
   `ord` below; Neon's `aws-us-east-2` is closest).
3. In the project dashboard, go to **Settings → Extensions** and enable
   `vector`. (One toggle. Neon ships pgvector pre-installed; you just
   activate it.)
4. Copy the **Connection string** from the dashboard. It looks like:
   ```
   postgresql://USER:PASSWORD@ep-xyz-12345.us-east-2.aws.neon.tech/rag_psych?sslmode=require
   ```
5. Apply our schema. From your laptop:
   ```bash
   psql 'postgresql://USER:PASSWORD@ep-xyz-12345.us-east-2.aws.neon.tech/rag_psych?sslmode=require' \
     -f ingest/schema.sql
   ```

---

## 2. Install Fly's CLI and authenticate

```bash
brew install flyctl              # macOS; see fly.io/docs/hands-on/install-flyctl/ for others
fly auth signup                  # or: fly auth login (browser)
```

Free trial credit covers the demo for the first month. After that the
runtime cost is dominated by uptime, which scale-to-zero pushes near zero.

---

## 3. Launch the Fly app (no deploy yet)

From the repo root:

```bash
fly launch --no-deploy --copy-config --name rag-psych-<your-suffix>
```

When prompted:
- **Region**: pick the same one your Neon DB is closest to (e.g. `ord`)
- **Postgres**: **No** (we're using Neon, not Fly Postgres)
- **Redis**: No
- **Settings**: keep the existing `fly.toml` (the `--copy-config` flag preserves it)

Open the generated `fly.toml` and update the `app = "..."` line to match
the unique name Fly assigned (the launcher overwrites it).

---

## 4. Set secrets

These never appear in `fly.toml` or the image. They live encrypted in
Fly's secret store and are injected as env vars at runtime.

```bash
fly secrets set \
  DATABASE_URL='postgresql://USER:PASSWORD@ep-xyz.us-east-2.aws.neon.tech/rag_psych?sslmode=require' \
  ANTHROPIC_API_KEY='sk-ant-api03-...' \
  ANTHROPIC_MODEL='claude-haiku-4-5' \
  NCBI_EMAIL='you@example.com' \
  ICD_CLIENT_ID='...' \
  ICD_CLIENT_SECRET='...' \
  EVAL_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')" \
  CORS_ORIGIN='https://rag-psych-<your-suffix>.fly.dev'
```

> ⚠️ **Rotate your local `.env` keys before deploying.** The
> `ANTHROPIC_API_KEY` and `EVAL_PASSWORD` currently in `.env` should be
> regenerated for production — assume the old ones are compromised
> (anything that touches a chat with an LLM is). Console:
> [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys).

---

## 5. Deploy

```bash
fly deploy
```

First push uploads ~3.5 GB (models baked into the image — see
`api/Dockerfile`). Subsequent deploys only push the layers that changed,
so code edits redeploy in seconds.

When it finishes:

```bash
fly status                        # check machine health
fly logs                          # follow startup logs
open https://rag-psych-<your-suffix>.fly.dev/health
```

A `{"status":"ok"}` response means the API is up. The first `/query`
will pay a 5–10 s cold-start while the embedder + reranker load.

---

## 6. Seed the remote database

The Neon DB is empty after step 1. Run ingest from your laptop against it:

```bash
DATABASE_URL='postgresql://USER:PASSWORD@ep-xyz.us-east-2.aws.neon.tech/rag_psych?sslmode=require' \
  ./scripts/seed_remote.sh
```

This takes 10–15 minutes wall time, mostly the PubMed `efetch` loop on
the first run. The cached JSON files in `data/cache/` mean re-runs are
near-instant.

After it finishes, hit the deployed UI:

```bash
open https://rag-psych-<your-suffix>.fly.dev/ui
```

---

## 7. Production hardening (recommended before sharing publicly)

| Item | How |
|---|---|
| Anthropic spend cap | Drop to **$5/week** at console.anthropic.com → Settings → Limits before sharing the URL |
| `/eval` password | Confirm `EVAL_PASSWORD` is random + long. Never the local-dev value. |
| Rate limit | Already 30/min/IP. If you see abuse, drop to 10/min in `api/main.py`'s `@limiter.limit` decorator and `fly deploy` |
| CORS | Only the Fly subdomain in production. If you remove the localhost dev origin from the Fly secret, dev still works locally because your `.env` has its own value |
| Healthcheck grace | `fly.toml` already gives 30 s for cold-start. If you see flapping, bump to 60 s |
| Auto-stop | Already on (`auto_stop_machines = "stop"`). Verify with `fly status` — idle machines should report `stopped` |

---

## 8. Day-2 ops cheatsheet

```bash
fly status                       # which machines are running
fly logs                         # tail combined logs
fly logs --instance <id>         # one machine
fly ssh console                  # shell into a running instance
fly secrets list                 # what's set (values not shown)
fly secrets unset SOME_KEY       # remove
fly scale memory 4096            # bump RAM if rerank latency is bad
fly scale count 2                # add a second machine for redundancy
fly apps destroy rag-psych-<...> # nuke everything (careful)
```

---

## 9. Things you'll feel in production that you don't on localhost

- **First request is slow** (5–10 s) when the machine just woke from
  auto-stop. Subsequent requests in the same minute are fast.
- **Latency floor is higher** because every request crosses the public
  internet (Fly ↔ Neon) instead of localhost. Expect ~2× the times shown
  in `eval/results/*.json`.
- **Anthropic costs scale with usage.** A naive demo URL hit by 100 curious
  visitors at 5 queries each = ~$2 of Haiku tokens. Spend cap protects you.
- **No DSM in answers.** If a query that worked locally suddenly returns
  the refusal string in production, you're hitting the DSM-shaped hole in
  the public corpus — that's expected.

---

## Why not Fly Postgres?

Fly's managed Postgres is fine but you'd have to install pgvector via a
custom Postgres image and manage it yourself, then pay $5/mo for the
smallest instance. Neon's free tier is functionally equivalent for our
scale (~30K chunks) and has zero setup beyond toggling the extension.

If you want everything inside Fly's network later (lower latency,
fewer external dependencies), swap `DATABASE_URL` to a Fly Postgres
endpoint and re-run step 6. The application code doesn't care.
