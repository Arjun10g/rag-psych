# Deploying rag-psych to Hugging Face Spaces

Architecture: **Hugging Face Spaces (free CPU tier) for the API container, Neon
for Postgres + pgvector.** Both have free tiers — no credit card required for
either. The Space sleeps after 48 h of inactivity and wakes on the next
request (5–10 s cold-start while the embedder + reranker load).

> **Critical:** never run `--sources dsm5` against the remote DB. The DSM
> chunks are licensed for local personal use only and must stay in your
> laptop's `pgdata` volume. The `scripts/seed_remote.sh` helper rejects
> any attempt to ingest DSM remotely.

---

## 1. Provision Postgres on Neon (free)

1. Sign up at [console.neon.tech](https://console.neon.tech) (GitHub auth, free).
2. Create a new project. Region: pick `aws-us-east-1` or `aws-us-east-2` —
   HF Spaces' shared CPUs sit on AWS east, so co-locating keeps API↔DB
   latency under 30 ms.
3. Apply our schema. From the Neon dashboard's **SQL Editor** paste the
   contents of `ingest/schema.sql` and run. The first statement
   (`CREATE EXTENSION IF NOT EXISTS vector;`) auto-enables pgvector — no
   separate toggle needed on Neon's free plan.
4. Copy the **Connection string** from the dashboard. It looks like:
   ```
   postgresql://USER:PASSWORD@ep-xyz-12345.us-east-2.aws.neon.tech/rag_psych?sslmode=require
   ```

---

## 2. Create the Hugging Face Space

1. Sign in at [huggingface.co](https://huggingface.co) (free, GitHub auth works).
2. Go to **New Space** → fill in:
   - **Owner**: your username
   - **Space name**: `rag-psych` (or whatever)
   - **License**: MIT
   - **SDK**: **Docker** (NOT Gradio/Streamlit — we have our own FastAPI app)
   - **Hardware**: **CPU basic — Free** (16 GB RAM, 2 vCPU, $0/mo)
   - **Visibility**: Public or Private — your call
3. Click **Create Space**. HF gives you a git remote like:
   ```
   https://huggingface.co/spaces/<your-username>/rag-psych
   ```

---

## 3. Push the code to the Space

The repo already contains everything HF needs:
- `Dockerfile` at the root (HF auto-detects)
- `README.md` with the YAML frontmatter HF reads (`sdk: docker`, `app_port: 7860`)

```bash
cd "path/to/rag-psych"
git init -b main                            # if not already a git repo
git add .
git commit -m "Initial deploy to HF Spaces"
git remote add space https://huggingface.co/spaces/<your-username>/rag-psych
git push space main
```

The first push uploads ~50 MB of code (no models — they're baked into the
image at build time on HF's side). HF then runs `docker build` on their
infra; expect 8–12 minutes the first time (most of it is the
sentence-transformers + cross-encoder warm-up). Subsequent pushes only
rebuild changed layers.

> If git asks for credentials, use your HF username + an **access token**
> from [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)
> (scope: `write`). The token replaces the password.

---

## 4. Set secrets (Variables and Secrets)

Open your Space → **Settings** → **Variables and secrets**.

**Secrets** (encrypted, never visible after save):

| Name | Value |
|---|---|
| `DATABASE_URL` | `postgresql://USER:PASSWORD@ep-xyz.us-east-2.aws.neon.tech/rag_psych?sslmode=require` |
| `ANTHROPIC_API_KEY` | `sk-ant-api03-...` |
| `NCBI_EMAIL` | `you@example.com` |
| `ICD_CLIENT_ID` | from [icd.who.int/icdapi](https://icd.who.int/icdapi) |
| `ICD_CLIENT_SECRET` | "" |
| `EVAL_PASSWORD` | `python3 -c "import secrets; print(secrets.token_urlsafe(16))"` |

**Variables** (visible in the Space settings, but not in the image):

| Name | Value |
|---|---|
| `PORT` | `7860` |
| `ANTHROPIC_MODEL` | `claude-haiku-4-5` |
| `CORS_ORIGIN` | `https://<your-username>-rag-psych.hf.space` |
| `LOG_LEVEL` | `INFO` |

> ⚠️ **Rotate your local `.env` keys before deploying.** The
> `ANTHROPIC_API_KEY` and `EVAL_PASSWORD` currently in `.env` should be
> regenerated for production — assume the old ones are compromised
> (anything that touches a chat with an LLM is). Console:
> [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys).

After saving secrets, the Space auto-restarts.

---

## 5. Seed the remote database

The Neon DB is empty after step 1. Run ingest from your laptop against it
(do **not** ingest from the Space — it has no shell access on the free tier):

```bash
DATABASE_URL='postgresql://USER:PASSWORD@ep-xyz.us-east-2.aws.neon.tech/rag_psych?sslmode=require' \
  ./scripts/seed_remote.sh
```

This takes 10–15 minutes wall time, mostly the PubMed `efetch` loop on
the first run. The cached JSON files in `data/cache/` mean re-runs are
near-instant.

---

## 6. Verify it's up

Once the Space build finishes (watch the **Logs** tab), hit:

```
https://<your-username>-rag-psych.hf.space/health
```

A `{"status":"ok"}` response means the API is up. The first `/query`
will pay a 5–10 s cold-start while the embedder + reranker load.

The UI:
```
https://<your-username>-rag-psych.hf.space/ui
```

The eval dashboard (HTTP Basic — username can be anything, password is
your `EVAL_PASSWORD`):
```
https://<your-username>-rag-psych.hf.space/eval
```

---

## 7. Production hardening (recommended before sharing publicly)

| Item | How |
|---|---|
| Anthropic spend cap | Drop to **$5/week** at console.anthropic.com → Settings → Limits before sharing the URL |
| `/eval` password | Confirm `EVAL_PASSWORD` is random + long. Never the local-dev value. |
| Rate limit | Already 30/min/IP. If you see abuse, drop to 10/min in `api/main.py`'s `@limiter.limit` decorator and re-push |
| CORS | Only the Space subdomain in production. Keep `localhost:8000` out of the Space's `CORS_ORIGIN`. Local dev still works because your `.env` has its own value |
| Visibility | If you only want to share with specific reviewers, set Space to **Private** in Settings; share via direct link with HF auth |

---

## 8. Day-2 ops

| Action | How |
|---|---|
| Tail logs | Space page → **Logs** tab (live SSE stream) |
| Restart container | Settings → **Restart this Space** |
| Update code | `git push space main` — auto-rebuilds |
| Update secrets | Settings → Variables and secrets → edit → Save (auto-restart) |
| Pause to save resources | Settings → **Pause this Space** (preserves state, manual resume) |
| Sleep schedule | Free tier auto-sleeps after **48 h of no traffic**; first request after sleep wakes it (10–20 s) |
| Upgrade hardware | Settings → Hardware → pick a paid tier ($0.03/hr CPU upgrade, $0.60/hr T4 GPU). Not needed for this app |
| Delete | Settings → **Delete this Space** (irreversible) |

---

## 9. Things you'll feel in production that you don't on localhost

- **First request after sleep is slow** (10–20 s). The Space wakes,
  Docker container starts, embedder + reranker load. Subsequent requests
  in the same hour are fast.
- **Latency floor is higher** because every request crosses the public
  internet (HF ↔ Neon) instead of localhost. Expect ~2× the times shown
  in `eval/results/*.json`.
- **Anthropic costs scale with usage.** A naive demo URL hit by 100 curious
  visitors at 5 queries each = ~$2 of Haiku tokens. Spend cap protects you.
- **No DSM in answers.** If a query that worked locally suddenly returns
  the refusal string in production, you're hitting the DSM-shaped hole in
  the public corpus — that's expected.

---

## Why HF Spaces over Fly.io / Railway / Render?

| Provider | Free tier? | RAM ceiling | Card required? | Verdict |
|---|---|---|---|---|
| **HF Spaces** | Yes — 16 GB CPU basic | 16 GB | No | ✅ Fits comfortably |
| Fly.io | No (killed Oct 2024) | n/a | Yes | ❌ Not free anymore |
| Railway | $5/mo trial credit | 8 GB | Yes (after trial) | ❌ Card required |
| Render free | Yes | **512 MB** | No | ❌ Embedder + reranker need ~1.5 GB; OOM |

HF Spaces is the only free tier with enough RAM for our model stack and
no payment instrument required.
