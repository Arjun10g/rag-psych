"""
ICD-11 — WHO International Classification of Diseases, 11th revision.
Specifically the Mental, Behavioural or Neurodevelopmental Disorders chapter
(Chapter 06 in the MMS linearization).

API docs:    https://icd.who.int/icdapi
API base:    https://id.who.int/icd/
Token URL:   https://icdaccessmanagement.who.int/connect/token
Chapter URI: https://id.who.int/icd/release/11/mms/334423054

Auth: OAuth2 client credentials. ICD_CLIENT_ID / ICD_CLIENT_SECRET in .env.
      Tokens last ~1h; we cache to .token.json with the expiry timestamp
      and refresh on 401 from any endpoint.

Headers required by the API:
  Authorization: Bearer <token>
  Accept: application/json
  Accept-Language: en
  API-Version: v2

Walker: BFS-ish DFS from the chapter URI, following the `child` array on
each entity. Visited entity_ids are tracked so the multi-parent shape of
ICD-11 doesn't cause re-fetches or duplicate documents.

Caching:
  data/cache/icd11/{entity_id}.json   — one file per entity
  data/cache/icd11/.token.json        — OAuth token + expiry

Chunking: one chunk per ICD-11 field (Definition, Additional Information,
Diagnostic Requirements, Inclusion, etc.). Field name becomes the chunk
`section`. Long fields sub-split with overlap.

DO NOT:
  - Log client_id / client_secret / access tokens at any level.
  - Skip the polite 100ms inter-request sleep on uncached fetches.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Iterator

import requests

from . import Chunk, RawDocument

logger = logging.getLogger(__name__)

TOKEN_URL = "https://icdaccessmanagement.who.int/connect/token"
TOKEN_SCOPE = "icdapi_access"
TOKEN_SAFETY_BUFFER_SECONDS = 60
REQUEST_DELAY_SECONDS = 0.1
REQUEST_TIMEOUT_SECONDS = 30
BASE_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en",
    "API-Version": "v2",
}

CHUNK_MAX_CHARS = 1500
CHUNK_OVERLAP_CHARS = 150

MENTAL_DISORDERS_CHAPTER_URI = (
    "https://id.who.int/icd/release/11/mms/334423054"
)

# (api_key, human_label) — order is the document's reading order.
TEXT_FIELDS: tuple[tuple[str, str], ...] = (
    ("definition", "Definition"),
    ("longDefinition", "Long Definition"),
    ("fullySpecifiedName", "Fully Specified Name"),
    ("additionalInformation", "Additional Information"),
    ("diagnosticCriteria", "Diagnostic Requirements"),
    ("codingNote", "Coding Note"),
    ("codingHint", "Coding Hint"),
)

LIST_FIELDS: tuple[tuple[str, str], ...] = (
    ("inclusion", "Inclusion"),
    ("exclusion", "Exclusion"),
    ("synonym", "Synonyms"),
    ("narrowerTerm", "Narrower Terms"),
    ("indexTerm", "Index Terms"),
)


class ICD11Source:
    source_type = "icd11"

    def __init__(
        self,
        chapter_uri: str = MENTAL_DISORDERS_CHAPTER_URI,
        cache_dir: Path | None = None,
    ) -> None:
        self.chapter_uri = chapter_uri
        self.cache_dir = cache_dir or Path("data/cache/icd11")

    def load(self) -> Iterator[RawDocument]:
        """Walk Chapter 06 and yield one RawDocument per entity."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        client = _ICDClient(self.cache_dir)
        seen: set[str] = set()
        stack = [self.chapter_uri]
        while stack:
            uri = stack.pop()
            entity_id = _entity_id_from_uri(uri)
            if entity_id in seen:
                continue
            seen.add(entity_id)
            entity = client.get_entity(uri)
            if entity is None:
                continue
            for child_uri in entity.get("child", []) or []:
                stack.append(child_uri)
            doc = _entity_to_document(uri, entity_id, entity)
            if doc is not None:
                yield doc

    def chunk(self, doc: RawDocument) -> Iterator[Chunk]:
        sections = doc.metadata.get("sections") or []
        idx = 0
        for label, body in sections:
            body = (body or "").strip()
            if not body:
                continue
            for piece in _split(body):
                yield Chunk(section=label, chunk_index=idx, text=piece)
                idx += 1


class _ICDClient:
    """OAuth2 + on-disk-cached HTTP client for the ICD-11 API."""

    def __init__(self, cache_dir: Path) -> None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = cache_dir
        self.token_path = cache_dir / ".token.json"
        self._token: str | None = None
        self._expiry: float = 0.0

    def get_entity(self, uri: str) -> dict[str, Any] | None:
        entity_id = _entity_id_from_uri(uri)
        cache_file = self.cache_dir / f"{entity_id}.json"
        if cache_file.exists():
            return json.loads(cache_file.read_text())

        data = self._fetch(uri)
        if data is None:
            return None
        # The release-unaware MMS URI returns a release-index, not the
        # actual entity content. Follow `latestRelease` once to land on
        # the real entity. Subsequent walks already use release URIs.
        if "child" not in data and "definition" not in data and data.get("latestRelease"):
            data = self._fetch(data["latestRelease"]) or data
        cache_file.write_text(json.dumps(data, ensure_ascii=False))
        return data

    def _fetch(self, uri: str) -> dict[str, Any] | None:
        time.sleep(REQUEST_DELAY_SECONDS)
        r = requests.get(uri, headers=self._auth_headers(), timeout=REQUEST_TIMEOUT_SECONDS)
        if r.status_code == 401:
            self._token = None
            r = requests.get(uri, headers=self._auth_headers(), timeout=REQUEST_TIMEOUT_SECONDS)
        if r.status_code == 404:
            logger.warning("404 for %s, skipping", uri)
            return None
        r.raise_for_status()
        return r.json()

    def _auth_headers(self) -> dict[str, str]:
        return {**BASE_HEADERS, "Authorization": f"Bearer {self._get_token()}"}

    def _get_token(self) -> str:
        now = time.time()
        if self._token and now < self._expiry - TOKEN_SAFETY_BUFFER_SECONDS:
            return self._token
        if self.token_path.exists():
            cached = json.loads(self.token_path.read_text())
            if now < cached.get("expiry", 0) - TOKEN_SAFETY_BUFFER_SECONDS:
                self._token = cached["access_token"]
                self._expiry = cached["expiry"]
                return self._token
        return self._refresh_token()

    def _refresh_token(self) -> str:
        client_id = os.environ.get("ICD_CLIENT_ID", "").strip()
        client_secret = os.environ.get("ICD_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise RuntimeError(
                "ICD_CLIENT_ID and ICD_CLIENT_SECRET must be set in .env"
            )
        r = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": TOKEN_SCOPE,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        r.raise_for_status()
        body = r.json()
        token = body["access_token"]
        expires_in = int(body.get("expires_in", 3600))
        self._token = token
        self._expiry = time.time() + expires_in
        self.token_path.write_text(
            json.dumps({"access_token": token, "expiry": self._expiry})
        )
        return token


def _entity_id_from_uri(uri: str) -> str:
    return uri.rstrip("/").split("/")[-1]


def _entity_to_document(
    uri: str, entity_id: str, entity: dict[str, Any]
) -> RawDocument | None:
    title = _extract_value(entity.get("title"))
    if not title:
        return None

    sections: list[tuple[str, str]] = []
    for key, label in TEXT_FIELDS:
        val = _extract_value(entity.get(key))
        if val:
            sections.append((label, val))
    for key, label in LIST_FIELDS:
        items = _extract_label_list(entity.get(key))
        if items:
            sections.append((label, "- " + "\n- ".join(items)))

    text = "\n\n".join(f"{label}:\n{body}" for label, body in sections)
    if not text:
        text = title

    return RawDocument(
        source_type="icd11",
        source_id=entity_id,
        title=title,
        text=text,
        license="WHO-ICD-API-TOS",
        source_uri=uri,
        metadata={
            "code": entity.get("code"),
            "browserUrl": entity.get("browserUrl"),
            "parent_uris": entity.get("parent", []) or [],
            "child_count": len(entity.get("child", []) or []),
            "sections": sections,
        },
    )


def _extract_value(node: Any) -> str:
    """Pull a printable string out of ICD-11's multi-lingual value shapes."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node.strip()
    if isinstance(node, dict):
        if "@value" in node:
            return str(node["@value"]).strip()
        if "label" in node:
            return _extract_value(node["label"])
        if "value" in node:
            return str(node["value"]).strip()
    return ""


def _extract_label_list(node: Any) -> list[str]:
    if not isinstance(node, list):
        return []
    out: list[str] = []
    for item in node:
        if isinstance(item, dict):
            v = _extract_value(item.get("label") or item)
            if v:
                out.append(v)
    return out


def _split(text: str) -> Iterator[str]:
    text = (text or "").strip()
    if not text:
        return
    if len(text) <= CHUNK_MAX_CHARS:
        yield text
        return
    parts = re.split(r"(?<=[.!?])\s+", text)
    buf = ""
    for part in parts:
        if not part:
            continue
        if len(part) > CHUNK_MAX_CHARS:
            if buf:
                yield buf
                buf = ""
            yield from _hard_window(part)
            continue
        if not buf:
            buf = part
        elif len(buf) + 1 + len(part) <= CHUNK_MAX_CHARS:
            buf += " " + part
        else:
            yield buf
            buf = part
    if buf:
        yield buf


def _hard_window(text: str) -> Iterator[str]:
    step = CHUNK_MAX_CHARS - CHUNK_OVERLAP_CHARS
    for start in range(0, len(text), step):
        piece = text[start:start + CHUNK_MAX_CHARS]
        if piece:
            yield piece
        if start + CHUNK_MAX_CHARS >= len(text):
            break
