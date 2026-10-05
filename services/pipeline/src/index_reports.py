#!/usr/bin/env python3
"""
보안 진단 보고서 MD → Qdrant 인덱싱
컬렉션: security_reports
임베딩: nomic-embed-text

실행:
  docker run --rm \\
    --network dolphin_ai_dolphin-net \\
    --add-host host.docker.internal:host-gateway \\
    -v /path/to/your/reports:/reports:ro \\
    -e QDRANT_URL=http://qdrant:6333 \\
    -e OLLAMA_URL=http://host.docker.internal:11434 \\
    dolphin_ai-pipeline:latest \\
    python /app/src/index_reports.py
"""

import os
import re
import sys
import hashlib
from pathlib import Path
from typing import List, Dict, Any

import httpx
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct,
    PayloadSchemaType,
)

QDRANT_URL   = os.environ.get("QDRANT_URL",   "http://qdrant:6333")
OLLAMA_URL   = os.environ.get("OLLAMA_URL",   "http://host.docker.internal:11434")
EMBED_MODEL  = "nomic-embed-text"
COLLECTION   = "security_reports"
VECTOR_SIZE  = 768
CHUNK_WORDS  = 250
OVERLAP_WORDS = 40
REPORT_DIR   = Path(os.environ.get("REPORT_DIR", "/reports"))


# ── 청크 분할 ────────────────────────────────────────────

def split_chunks(text: str) -> List[str]:
    """취약점 섹션 경계 우선, 아니면 word window로 분할."""
    # 마크다운 헤더(##, ###)로 먼저 분할
    sections = re.split(r"(?=\n#{1,3} )", text)
    chunks: List[str] = []

    for sec in sections:
        words = sec.split()
        if len(words) <= CHUNK_WORDS:
            if sec.strip():
                chunks.append(sec.strip())
        else:
            # 긴 섹션은 overlap window로 추가 분할
            for i in range(0, len(words), CHUNK_WORDS - OVERLAP_WORDS):
                piece = " ".join(words[i : i + CHUNK_WORDS])
                if piece.strip():
                    chunks.append(piece.strip())

    return chunks


# ── 파일 메타데이터 추출 ─────────────────────────────────

def extract_meta(path: Path) -> Dict[str, Any]:
    """파일명에서 클라이언트명, 진단 유형 추론."""
    name = path.stem  # 확장자 제거
    meta: Dict[str, Any] = {
        "filename": path.name,
        "client": "",
        "diag_type": "",
        "is_followup": False,
    }

    # "이행점검" 여부
    if "이행점검" in name:
        meta["is_followup"] = True
        meta["diag_type"] = "이행점검"
    elif "취약점진단" in name or "정적" in name:
        meta["diag_type"] = "취약점진단"

    # 첫 번째 마침표 앞이 클라이언트 힌트
    parts = name.split(" - ")
    if len(parts) >= 2:
        meta["client"] = parts[0].strip()

    return meta


# ── 임베딩 ───────────────────────────────────────────────

def _embed_one(text: str) -> "List[float] | None":
    """단일 텍스트 임베딩.
    500 에러 시 반으로 나눠 재귀 시도 (토큰 초과 대응).
    10단어 이하까지 내려가도 실패하면 None 반환 (Ollama 일시 크래시 대응).
    """
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
                return None  # 바닥 도달 — 이 mini-chunk만 스킵
            half = len(words) // 2
            left  = _embed_one(" ".join(words[:half]))
            right = _embed_one(" ".join(words[half:]))
            if left is None or right is None:
                return None
            return [(a + b) / 2 for a, b in zip(left, right)]
        raise


def embed(texts: List[str]) -> "List[List[float] | None]":
    """청크 리스트 임베딩. 각 청크는 실패 시 None."""
    return [_embed_one(t) for t in texts]


# ── 메인 ─────────────────────────────────────────────────

def main():
    if not REPORT_DIR.exists():
        print(f"ERROR: {REPORT_DIR} 없음", file=sys.stderr)
        sys.exit(1)

    md_files = sorted(REPORT_DIR.glob("*.md"))
    print(f"MD 파일 {len(md_files)}개 발견 → {COLLECTION} 인덱싱 시작")

    client = QdrantClient(url=QDRANT_URL, timeout=30)

    # 컬렉션 없으면 생성
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

    for idx, md_path in enumerate(md_files, 1):
        text = md_path.read_text(encoding="utf-8", errors="ignore")
        if not text.strip():
            print(f"  [{idx}/{len(md_files)}] SKIP (빈 파일): {md_path.name}")
            continue

        meta = extract_meta(md_path)
        chunks = split_chunks(text)
        print(f"  [{idx}/{len(md_files)}] {md_path.name} → {len(chunks)}청크", end="", flush=True)

        try:
            vectors = embed(chunks)
        except Exception as e:
            print(f" ERROR (임베딩 실패): {e}")
            continue

        points = []
        skipped = 0
        for ci, (chunk, vec) in enumerate(zip(chunks, vectors)):
            if vec is None:
                skipped += 1
                continue
            uid = int(hashlib.md5(f"{md_path.name}_{ci}".encode()).hexdigest()[:15], 16)
            points.append(PointStruct(
                id=uid,
                vector=vec,
                payload={
                    **meta,
                    "chunk_index": ci,
                    "text": chunk,
                },
            ))

        BATCH = 200
        for i in range(0, len(points), BATCH):
            client.upsert(collection_name=COLLECTION, points=points[i:i+BATCH])
        total_points += len(points)
        suffix = f" (스킵 {skipped}청크)" if skipped else ""
        print(f" ✓{suffix}")

    print(f"\n완료: {len(md_files)}파일 / {total_points}포인트 → {COLLECTION}")


if __name__ == "__main__":
    main()
