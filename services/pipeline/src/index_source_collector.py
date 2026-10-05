#!/usr/bin/env python3
"""
Source_Collector raw/ 전체 → Qdrant 인덱싱
컬렉션: security_kb
임베딩: nomic-embed-text (768차원)

실행:
  docker run --rm \\
    --network dolphin_ai_dolphin-net \\
    --add-host host.docker.internal:host-gateway \\
    -v /path/to/your/source-collection:/data:ro \\
    -e QDRANT_URL=http://qdrant:6333 \\
    -e OLLAMA_URL=http://host.docker.internal:11434 \\
    dolphin_ai-pipeline:latest \\
    python /app/src/index_source_collector.py
"""

import os
import sys
import hashlib
import re
import time
from pathlib import Path
from typing import List, Dict, Any, Optional

import httpx
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

QDRANT_URL  = os.environ.get("QDRANT_URL", "http://qdrant:6333")
OLLAMA_URL  = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
EMBED_MODEL = "nomic-embed-text"
COLLECTION  = "security_kb"
VECTOR_SIZE = 768
CHUNK_WORDS = 300
OVERLAP_WORDS = 50
DATA_DIR    = Path(os.environ.get("DATA_DIR", "/data"))
CHECKPOINT  = Path(os.environ.get("CHECKPOINT_FILE", "/tmp/index_checkpoint.txt"))

GITHUB_DIR = DATA_DIR / "raw" / "github"
WEB_DIR    = DATA_DIR / "raw" / "web"

# 수집 대상 디렉토리 → 카테고리 매핑
CATEGORY_MAP = {
    "codeql-security-md": "static_analysis",
    "vulndb-md":          "advisory_go",
    "advisory-database-md": "advisory_python",
    "rustsec":            "advisory_rust",
    "rubysec":            "advisory_ruby",
    "vulhub":             "vuln_env",
    "OWASP":              "owasp",
    "spring-projects":    "framework_security",
    "openssl":            "crypto",
    "pyca":               "crypto",
    "slsa-framework":     "supply_chain",
    "ossf":               "supply_chain",
    "sigstore":           "supply_chain",
    "bridgecrewio":       "iac_security",
    "aquasecurity":       "container_security",
    "PyCQA":              "static_analysis",
    "securego":           "static_analysis",
    "presidentbeef":      "static_analysis",
    "spotbugs":           "static_analysis",
    "nodesecurity":       "static_analysis",
    "Azure":              "ai_security",
    "microsoft":          "ai_security",
    "toniblyx":           "cloud_security",
    "nccgroup":           "cloud_security",
}


def infer_category(path: Path) -> str:
    parts = path.parts
    for p in parts:
        if p in CATEGORY_MAP:
            return CATEGORY_MAP[p]
    # web 디렉토리 기반
    if "web" in parts:
        idx = list(parts).index("web")
        if idx + 1 < len(parts):
            sub = parts[idx + 1]
            cat_map = {
                "nist": "compliance", "attck": "threat_intel",
                "cisa": "compliance", "cwe": "vulnerability",
                "nvd": "vulnerability", "crypto": "crypto",
                "ai-security": "ai_security", "supply-chain": "supply_chain",
                "java": "framework_security", "guides": "best_practices",
                "android-security": "mobile_security", "oauth-rfc": "auth",
                "hipaa": "compliance", "owasp-serverless": "cloud_security",
            }
            return cat_map.get(sub, "general")
    return "general"


def split_chunks(text: str) -> List[str]:
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


def collect_md_files() -> List[Path]:
    files: List[Path] = []
    # GitHub md
    if GITHUB_DIR.exists():
        files.extend(GITHUB_DIR.rglob("*.md"))
    # Web md + txt
    if WEB_DIR.exists():
        files.extend(WEB_DIR.rglob("*.md"))
        files.extend(WEB_DIR.rglob("*.txt"))
    return [f for f in files if f.stat().st_size > 100]


def main():
    if not DATA_DIR.exists():
        print(f"ERROR: {DATA_DIR} 없음", file=sys.stderr)
        sys.exit(1)

    all_files = collect_md_files()
    done = load_checkpoint()
    if done:
        print(f"체크포인트 {len(done)}개 로드 → 재개")
    print(f"파일 {len(all_files)}개 발견 → {COLLECTION} 인덱싱 시작")

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

    for idx, md_path in enumerate(all_files, 1):
        try:
            text = md_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            errors += 1
            continue

        if not text.strip():
            continue

        rel_path = str(md_path.relative_to(DATA_DIR))
        if rel_path in done:
            continue
        category = infer_category(md_path)
        chunks = split_chunks(text)
        if not chunks:
            continue

        prefix = f"[{idx}/{len(all_files)}] {rel_path}"
        print(f"  {prefix} → {len(chunks)}청크", end="", flush=True)

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
            uid = int(hashlib.md5(f"{rel_path}_{ci}".encode()).hexdigest()[:15], 16)
            points.append(PointStruct(
                id=uid,
                vector=vec,
                payload={
                    "source": rel_path,
                    "category": category,
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
            ck.write(rel_path + "\n")

    print(f"\n완료: {len(all_files)}파일 / {total_points}포인트 / 오류 {errors}건 → {COLLECTION}")


if __name__ == "__main__":
    main()
