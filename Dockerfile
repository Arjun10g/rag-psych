# syntax=docker/dockerfile:1.7
# rag-psych API container.
#
# Single Dockerfile used by:
#   - docker compose (local dev)              → listens on PORT (default 8000)
#   - Hugging Face Spaces (Docker SDK)        → set PORT=7860 in Space variables
#   - Fly.io                                  → fly.toml internal_port = 8000
#
# Notes:
# - python:3.11-slim base (CLAUDE.md pins 3.11)
# - Non-root user `rag` per CLAUDE.md rule 9; created BEFORE app code is copied
# - Models are pre-downloaded at build time so the first /query doesn't pay
#   a 30-90 s cold-load penalty inside the container. Tradeoff: image grows
#   by ~500 MB (Bio_PubMedBERT + ms-marco-MiniLM rerank).
# - Layer ordering: requirements.txt → pip install → model warm → app code,
#   so code edits don't bust the slow layers.

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/home/rag/.cache/huggingface \
    TRANSFORMERS_OFFLINE=0 \
    PORT=8000

# curl is used by the HEALTHCHECK; build-essential is dropped right after
# pip install completes so it doesn't bloat the final image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin rag
WORKDIR /app

# Dependencies first — separate layer so app-code edits don't reinstall.
COPY --chown=rag:rag requirements.txt ./
RUN pip install -r requirements.txt

# Pre-download the embedding + reranker models as the rag user so the
# weights land in the cache HF_HOME points at. Done at build time, runs
# offline at runtime.
USER rag
RUN python -c "\
from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer('pritamdeka/S-PubMedBert-MS-MARCO'); \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-12-v2'); \
print('models cached')"

# Application code — last layer so an edit only invalidates this step.
COPY --chown=rag:rag api ./api

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=4 \
  CMD curl -fs http://localhost:${PORT:-8000}/health || exit 1

CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
