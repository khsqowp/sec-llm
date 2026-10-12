#!/usr/bin/env python3
"""
OWASP Top 10 2021 → Qdrant security_kb indexer

실행 (Docker):
  docker run --rm \
    --network dolphin_ai_dolphin-net \
    -v /Users/user/Desktop/CODE/dolphin_ai/data/fastembed:/fastembed_cache \
    -v /Users/user/Desktop/CODE/dolphin_ai/services/pipeline/src:/app/src \
    -e QDRANT_URL=http://qdrant:6333 \
    -e FASTEMBED_CACHE_PATH=/fastembed_cache \
    dolphin_ai-pipeline:latest \
    python /app/src/index_owasp.py
"""

import hashlib
import os
import re
import sys
import time
from typing import Optional

import httpx
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue, ScrollRequest

QDRANT_URL         = os.environ.get("QDRANT_URL",          "http://qdrant:6333")
FASTEMBED_CACHE    = os.environ.get("FASTEMBED_CACHE_PATH", None)
EMBED_MODEL        = "nomic-ai/nomic-embed-text-v1"
COLLECTION         = os.environ.get("COLLECTION",           "security_kb")

CHUNK_WORDS   = 300
OVERLAP_WORDS = 50

OWASP_URLS = [
    "https://raw.githubusercontent.com/OWASP/Top10/master/2021/docs/en/A01_2021-Broken_Access_Control.md",
    "https://raw.githubusercontent.com/OWASP/Top10/master/2021/docs/en/A02_2021-Cryptographic_Failures.md",
    "https://raw.githubusercontent.com/OWASP/Top10/master/2021/docs/en/A03_2021-Injection.md",
    "https://raw.githubusercontent.com/OWASP/Top10/master/2021/docs/en/A04_2021-Insecure_Design.md",
    "https://raw.githubusercontent.com/OWASP/Top10/master/2021/docs/en/A05_2021-Security_Misconfiguration.md",
    "https://raw.githubusercontent.com/OWASP/Top10/master/2021/docs/en/A06_2021-Vulnerable_and_Outdated_Components.md",
    "https://raw.githubusercontent.com/OWASP/Top10/master/2021/docs/en/A07_2021-Identification_and_Authentication_Failures.md",
    "https://raw.githubusercontent.com/OWASP/Top10/master/2021/docs/en/A08_2021-Software_and_Data_Integrity_Failures.md",
    "https://raw.githubusercontent.com/OWASP/Top10/master/2021/docs/en/A09_2021-Security_Logging_and_Monitoring_Failures.md",
    "https://raw.githubusercontent.com/OWASP/Top10/master/2021/docs/en/A10_2021-Server-Side_Request_Forgery_(SSRF).md",
]


def split_chunks(text: str) -> list[str]:
    sections = re.split(r"(?=\n#{1,3} )", text)
    chunks: list[str] = []
    for sec in sections:
        words = sec.split()
        if len(words) <= CHUNK_WORDS:
            if sec.strip():
                chunks.append(sec.strip())
        else:
            for i in range(0, len(words), CHUNK_WORDS - OVERLAP_WORDS):
                piece = " ".join(words[i: i + CHUNK_WORDS])
                if piece.strip():
                    chunks.append(piece.strip())
    return chunks


_embedder = None

def _get_embedder():
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding
        kwargs = {"model_name": EMBED_MODEL}
        if FASTEMBED_CACHE:
            kwargs["cache_dir"] = FASTEMBED_CACHE
        _embedder = TextEmbedding(**kwargs)
    return _embedder


def _embed_one(text: str) -> Optional[list[float]]:
    try:
        embedder = _get_embedder()
        result = list(embedder.embed([text]))
        return result[0].tolist()
    except Exception as e:
        print(f"    embed error: {e}", flush=True)
        return None


def _is_indexed(client: QdrantClient, source_key: str) -> bool:
    result, _ = client.scroll(
        collection_name=COLLECTION,
        scroll_filter=Filter(
            must=[FieldCondition(key="source", match=MatchValue(value=source_key))]
        ),
        limit=1,
        with_payload=False,
        with_vectors=False,
    )
    return len(result) > 0


def _upsert_with_retry(client: QdrantClient, points: list, retries: int = 5):
    for attempt in range(retries):
        try:
            client.upsert(collection_name=COLLECTION, points=points)
            return
        except Exception as e:
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt
            print(f"    RETRY {attempt+1}/{retries} after {wait}s: {e}", flush=True)
            time.sleep(wait)


def main():
    client = QdrantClient(url=QDRANT_URL, timeout=30)

    existing = [c.name for c in client.get_collections().collections]
    if COLLECTION not in existing:
        print(f"ERROR: collection '{COLLECTION}' does not exist. Run index_guidelines.py first.", file=sys.stderr)
        sys.exit(1)

    print(f"Indexing {len(OWASP_URLS)} OWASP Top 10 2021 docs → {COLLECTION}")

    total_points = 0
    skipped_docs = 0

    with httpx.Client(timeout=30) as http:
        for url in OWASP_URLS:
            filename = url.split("/")[-1]
            source_key = f"owasp/{filename}"

            if _is_indexed(client, source_key):
                print(f"  SKIP (already indexed): {filename}")
                skipped_docs += 1
                continue

            print(f"  Fetching: {filename}", end="", flush=True)
            try:
                resp = http.get(url)
                resp.raise_for_status()
                text = resp.text
            except Exception as e:
                print(f" ERROR fetching: {e}")
                continue

            chunks = split_chunks(text)
            if not chunks:
                print(" (empty, skipped)")
                continue

            print(f" → {len(chunks)} chunks", end="", flush=True)

            from qdrant_client.models import PointStruct

            points = []
            for ci, chunk in enumerate(chunks):
                vec = _embed_one(chunk)
                if vec is None:
                    continue
                uid = int(hashlib.md5(f"owasp_{filename}_{ci}".encode()).hexdigest()[:15], 16)
                points.append(PointStruct(
                    id=uid,
                    vector=vec,
                    payload={
                        "source":   source_key,
                        "category": "owasp_top10",
                        "label":    "owasp_2021",
                        "text":     chunk,
                    },
                ))

            if points:
                _upsert_with_retry(client, points)
                total_points += len(points)
                print(f" ✓ ({len(points)} points)")
            else:
                print(" (no valid embeddings)")

    print(f"\nDone: {len(OWASP_URLS) - skipped_docs} docs indexed, {skipped_docs} skipped, {total_points} points total → {COLLECTION}")


if __name__ == "__main__":
    main()
