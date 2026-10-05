#!/usr/bin/env python3
"""
SHIELDUS_DOC/_md/guideline/ → Qdrant security_kb 인덱싱
SK Shieldus 보안 진단 가이드라인 (OS/DBMS/WEB/WAS/NW/클라우드)

실행:
  docker run --rm \
    --network dolphin_ai_dolphin-net \
    --add-host host.docker.internal:host-gateway \
    -v /path/to/your/guidelines:/guidelines:ro \
    -e QDRANT_URL=http://qdrant:6333 \
    -e OLLAMA_URL=http://host.docker.internal:11434 \
    dolphin_ai-pipeline:latest \
    python /app/src/index_guidelines.py
"""

import os
import re
import sys
import hashlib
import time
from pathlib import Path
from typing import List, Optional

import httpx
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

QDRANT_URL    = os.environ.get("QDRANT_URL",    "http://qdrant:6333")
OLLAMA_URL    = os.environ.get("OLLAMA_URL",    "http://host.docker.internal:11434")
EMBED_MODEL   = "nomic-embed-text"
COLLECTION    = "security_kb"
VECTOR_SIZE   = 768
CHUNK_WORDS   = 300
OVERLAP_WORDS = 50
GUIDELINE_DIR = Path(os.environ.get("GUIDELINE_DIR", "/guidelines"))
CHECKPOINT    = Path(os.environ.get("CHECKPOINT_FILE", "/tmp/guidelines_checkpoint.txt"))


def infer_category(filename: str) -> str:
    name = filename.lower()
    if "os진단" in name or "linux" in name or "windows" in name or "rocky" in name:
        return "pentest_os"
    if "db진단" in name or "oracle" in name or "mysql" in name or "mssql" in name \
            or "mariadb" in name or "postgresql" in name:
        return "pentest_db"
    if "web진단" in name or "apache" in name or "nginx" in name or "iis" in name:
        return "pentest_web"
    if "was진단" in name or "tomcat" in name:
        return "pentest_was"
    if "nw_" in name or "cisco" in name or "클라우드" in name or "aws" in name:
        return "pentest_network"
    if "보안장비" in name:
        return "pentest_security_device"
    if "개발보안" in name or "api" in name:
        return "secure_coding"
    return "pentest_general"


def split_chunks(text: str) -> List[str]:
    # 마크다운 헤더 경계 우선 분할, 이후 word window
    sections = re.split(r"(?=\n#{1,3} )", text)
    chunks: List[str] = []
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


def _embed_one(text: str) -> Optional[List[float]]:
    try:
        resp = httpx.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["embedding"]
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 500:
            words = text.split()
            if len(words) <= 10:
                return None
            half = len(words) // 2
            left  = _embed_one(" ".join(words[:half]))
            right = _embed_one(" ".join(words[half:]))
            if left is None or right is None:
                return None
            return [(a + b) / 2 for a, b in zip(left, right)]
        raise


def _upsert_with_retry(client: QdrantClient, points: list, retries: int = 5):
    for attempt in range(retries):
        try:
            client.upsert(collection_name=COLLECTION, points=points)
            return
        except Exception as e:
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt
            print(f"\n    RETRY {attempt+1}/{retries} after {wait}s: {e}", flush=True)
            time.sleep(wait)


def load_checkpoint() -> set:
    if CHECKPOINT.exists():
        return set(CHECKPOINT.read_text().splitlines())
    return set()


def main():
    if not GUIDELINE_DIR.exists():
        print(f"ERROR: {GUIDELINE_DIR} 없음", file=sys.stderr)
        sys.exit(1)

    md_files = sorted(GUIDELINE_DIR.glob("*.md"))
    md_files = [f for f in md_files if f.stat().st_size > 100]
    done = load_checkpoint()
    if done:
        print(f"체크포인트 {len(done)}개 로드 → 재개")
    print(f"가이드라인 {len(md_files)}개 발견 → {COLLECTION} 인덱싱 시작")

    client = QdrantClient(url=QDRANT_URL, timeout=30)

    existing = [c.name for c in client.get_collections().collections]
    if COLLECTION not in existing:
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        print(f"컬렉션 생성: {COLLECTION}")
    else:
        print(f"컬렉션 기존 사용: {COLLECTION}")

    total_points = 0
    errors = 0

    for idx, md_path in enumerate(md_files, 1):
        if md_path.name in done:
            continue

        try:
            text = md_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            errors += 1
            continue

        if not text.strip():
            continue

        category = infer_category(md_path.name)
        chunks = split_chunks(text)
        if not chunks:
            continue

        print(f"  [{idx}/{len(md_files)}] {md_path.name} ({category}) → {len(chunks)}청크", end="", flush=True)

        try:
            vectors = [_embed_one(c) for c in chunks]
        except Exception as e:
            print(f" ERROR: {e}")
            errors += 1
            continue

        points = []
        skipped = 0
        for ci, (chunk, vec) in enumerate(zip(chunks, vectors)):
            if vec is None:
                skipped += 1
                continue
            uid = int(hashlib.md5(f"guideline_{md_path.name}_{ci}".encode()).hexdigest()[:15], 16)
            points.append(PointStruct(
                id=uid,
                vector=vec,
                payload={
                    "source": f"guidelines/{md_path.name}",
                    "category": category,
                    "label": "guideline_skshieldus",
                    "filename": md_path.name,
                    "chunk_index": ci,
                    "text": chunk,
                },
            ))

        BATCH = 200
        for i in range(0, len(points), BATCH):
            _upsert_with_retry(client, points[i: i + BATCH])

        total_points += len(points)
        suffix = f" (스킵 {skipped})" if skipped else ""
        print(f" ✓{suffix}")
        with open(CHECKPOINT, "a") as ck:
            ck.write(md_path.name + "\n")

    print(f"\n완료: {len(md_files)}파일 / {total_points}포인트 / 오류 {errors}건 → {COLLECTION}")


if __name__ == "__main__":
    main()
