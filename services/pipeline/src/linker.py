"""
Qdrant 벡터 검색 + [[Wiki Link]] 자동 삽입 + 백링크 업데이트
Hybrid Search (dense nomic-embed-text + BM25 sparse) + Chunking
"""
import logging
import re
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

from fastembed import SparseTextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.http.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    Fusion,
    FusionQuery,
    MatchValue,
    PointStruct,
    Prefetch,
    SparseIndexParams,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from .config import Config
from .llm import LLMClient

logger = logging.getLogger(__name__)

COLLECTION = "notes"
VECTOR_SIZE = 768  # nomic-embed-text 출력 차원
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50


def _dense_embed_subprocess(cache_dir: str, texts: list) -> list:
    """Fresh subprocess에서 fastembed 실행 — ONNX 상태 완전 격리.

    relink_all_notes() 911회 embed 후 ONNX 세션이 오염된 상태에서
    index_note()를 호출하면 abort()가 발생하므로, reindex_missing()에서
    이 함수를 spawn subprocess로 실행해 깨끗한 ONNX 세션을 사용한다.
    """
    import os
    os.environ['OMP_NUM_THREADS'] = '1'
    os.environ['MKL_NUM_THREADS'] = '1'
    os.environ['OPENBLAS_NUM_THREADS'] = '1'
    os.environ['NUMEXPR_NUM_THREADS'] = '1'
    os.environ.setdefault('HF_HUB_DISABLE_XET', '1')
    from fastembed import TextEmbedding
    model = TextEmbedding(
        model_name="nomic-ai/nomic-embed-text-v1",
        cache_dir=cache_dir,
        providers=["CPUExecutionProvider"],
        threads=1,
    )
    return [v.tolist() for v in model.embed(texts)]


class Linker:
    def __init__(self, config: Config, llm: LLMClient):
        self.config = config
        self.llm = llm
        self.client = QdrantClient(url=config.qdrant_url, timeout=30)
        logger.info("BM25 sparse encoder 초기화 중 (첫 실행 시 모델 다운로드)...")
        self._sparse = SparseTextEmbedding(model_name="Qdrant/bm25")
        logger.info("BM25 준비 완료")
        self._ensure_collection()

    # ── 컬렉션 관리 ──────────────────────────────────────

    def _ensure_collection(self):
        existing = [c.name for c in self.client.get_collections().collections]
        needs_create = True

        if COLLECTION in existing:
            info = self.client.get_collection(COLLECTION)
            vectors_cfg = info.config.params.vectors
            if isinstance(vectors_cfg, dict) and "dense" in vectors_cfg:
                needs_create = False
                logger.debug("Qdrant 컬렉션 스키마 OK (hybrid dense+sparse)")
            else:
                logger.info("구 스키마 감지 → 컬렉션 재생성 (hybrid 마이그레이션)")
                self.client.delete_collection(COLLECTION)

        if needs_create:
            self.client.create_collection(
                collection_name=COLLECTION,
                vectors_config={"dense": VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE)},
                sparse_vectors_config={
                    "sparse": SparseVectorParams(index=SparseIndexParams(on_disk=False))
                },
            )
            logger.info(f"Qdrant 컬렉션 생성 (hybrid): {COLLECTION}")

    # ── Sparse 인코딩 ────────────────────────────────────

    def _sparse_encode(self, text: str) -> Tuple[List[int], List[float]]:
        result = list(self._sparse.embed([text]))[0]
        return result.indices.tolist(), result.values.tolist()

    # ── 청킹 ─────────────────────────────────────────────

    def _chunk_text(self, text: str) -> List[str]:
        """마크다운 섹션 기반 분할, 긴 섹션은 CHUNK_SIZE 자로 재분할."""
        sections = re.split(r"\n(?=## )", text)
        chunks: List[str] = []
        for section in sections:
            section = section.strip()
            if not section:
                continue
            if len(section) <= CHUNK_SIZE:
                chunks.append(section)
            else:
                start = 0
                while start < len(section):
                    end = min(start + CHUNK_SIZE, len(section))
                    chunk = section[start:end].strip()
                    if chunk:
                        chunks.append(chunk)
                    if end >= len(section):
                        break
                    start = end - CHUNK_OVERLAP
        return chunks or [text[:CHUNK_SIZE]]

    # ── 노트 인덱싱 ──────────────────────────────────────

    def index_note(self, note_path: Path, title: str, domain: str, content: str) -> str:
        """노트를 청크로 분할해 Qdrant에 인덱싱. note_id 반환."""
        self._reindex_log(f"[IDX-ENTER] {title}: index_note 진입")
        note_id = str(uuid.uuid4())
        self._reindex_log(f"[IDX-CHUNK] {title}: _chunk_text 호출 전")
        chunks = self._chunk_text(content)
        self._reindex_log(f"[IDX-CHUNK] {title}: _chunk_text 완료 ({len(chunks)}청크)")

        points: List[PointStruct] = []
        logger.info(f"[IDX] embed 시작: {title} ({len(chunks)}청크)")
        self._reindex_log(f"[IDX] {title}: {len(chunks)}청크 시작")
        for i, chunk_text in enumerate(chunks):
            logger.info(f"[IDX] chunk {i+1}/{len(chunks)} embed 중: {title}")
            self._reindex_log(f"[IDX] {title} chunk {i+1}/{len(chunks)} dense 시작")
            dense_vec = self.llm.embed(chunk_text)
            if not dense_vec:
                self._reindex_log(f"[IDX] {title} chunk {i+1}/{len(chunks)} dense None — 건너뜀")
                continue
            self._reindex_log(f"[IDX] {title} chunk {i+1}/{len(chunks)} dense OK — sparse 시작")
            sparse_idx, sparse_val = self._sparse_encode(chunk_text)
            self._reindex_log(f"[IDX] {title} chunk {i+1}/{len(chunks)} sparse OK")

            points.append(
                PointStruct(
                    id=str(uuid.uuid4()),
                    vector={
                        "dense": dense_vec,
                        "sparse": SparseVector(indices=sparse_idx, values=sparse_val),
                    },
                    payload={
                        "path": str(note_path),
                        "title": title,
                        "domain": domain,
                        "content": chunk_text,
                        "chunk_idx": i,
                        "total_chunks": len(chunks),
                        "note_id": note_id,
                    },
                )
            )

        if points:
            logger.info(f"[IDX] upsert 시작: {title} ({len(points)}청크)")
            self._reindex_log(f"[IDX] {title} upsert 시작 ({len(points)}청크)")
            self.client.upsert(collection_name=COLLECTION, points=points)
            logger.info(f"[IDX] upsert 완료: {title}")
            self._reindex_log(f"[IDX] {title} upsert 완료")

        return note_id

    def reindex_note(self, note_path: Path, title: str, domain: str, content: str) -> str:
        """기존 청크를 모두 삭제하고 재인덱싱."""
        try:
            self.client.delete(
                collection_name=COLLECTION,
                points_selector=FilterSelector(
                    filter=Filter(
                        must=[FieldCondition(key="path", match=MatchValue(value=str(note_path)))]
                    )
                ),
            )
        except Exception as e:
            logger.warning(f"청크 삭제 실패 ({note_path.name}): {e}")
        return self.index_note(note_path, title, domain, content)

    def precompute_index_embeddings(
        self, content: str
    ) -> List[Tuple[str, List[float], List[int], List[float]]]:
        """청크 임베딩 사전 계산 — nomic이 이미 로드된 상태에서 호출해 재로드 방지."""
        import time as _time
        chunks = self._chunk_text(content)
        result: List[Tuple[str, List[float], List[int], List[float]]] = []
        for i, chunk_text in enumerate(chunks):
            t0 = _time.time()
            logger.info(f"[TIMING] embed 시작 청크 {i+1}/{len(chunks)}")
            dense_vec = self.llm.embed(chunk_text)
            logger.info(f"[TIMING] embed 완료 청크 {i+1}/{len(chunks)}: {_time.time()-t0:.1f}s")
            if not dense_vec:
                continue
            sparse_idx, sparse_val = self._sparse_encode(chunk_text)
            result.append((chunk_text, dense_vec, sparse_idx, sparse_val))
        logger.info(f"[TIMING] 임베딩 사전 계산 완료: {len(result)}/{len(chunks)}청크")
        return result

    def reindex_note_precomputed(
        self,
        note_path: Path,
        title: str,
        domain: str,
        precomputed: List[Tuple[str, List[float], List[int], List[float]]],
    ) -> str:
        """사전 계산된 임베딩으로 재인덱싱 — Ollama 호출 없음."""
        import time as _time
        t0 = _time.time()
        logger.info(f"[TIMING] Qdrant DELETE 시작: {note_path.name}")
        try:
            self.client.delete(
                collection_name=COLLECTION,
                points_selector=FilterSelector(
                    filter=Filter(
                        must=[FieldCondition(key="path", match=MatchValue(value=str(note_path)))]
                    )
                ),
            )
            logger.info(f"[TIMING] Qdrant DELETE 완료: {_time.time()-t0:.1f}s")
        except Exception as e:
            logger.warning(f"청크 삭제 실패 ({note_path.name}): {e}")

        note_id = str(uuid.uuid4())
        total = len(precomputed)
        points: List[PointStruct] = []
        for i, (chunk_text, dense_vec, sparse_idx, sparse_val) in enumerate(precomputed):
            points.append(
                PointStruct(
                    id=str(uuid.uuid4()),
                    vector={
                        "dense": dense_vec,
                        "sparse": SparseVector(indices=sparse_idx, values=sparse_val),
                    },
                    payload={
                        "path": str(note_path),
                        "title": title,
                        "domain": domain,
                        "content": chunk_text,
                        "chunk_idx": i,
                        "total_chunks": total,
                        "note_id": note_id,
                    },
                )
            )

        if points:
            t1 = _time.time()
            logger.info(f"[TIMING] Qdrant UPSERT 시작: {len(points)}포인트")
            self.client.upsert(collection_name=COLLECTION, points=points)
            logger.info(f"[TIMING] Qdrant UPSERT 완료: {_time.time()-t1:.1f}s (전체: {_time.time()-t0:.1f}s)")
        return note_id

    # ── 페이로드 갱신 ────────────────────────────────────

    def update_point_payload(self, old_path: str, new_path: str) -> None:
        """파일 이동 후 Qdrant 포인트의 path 페이로드를 갱신 (모든 청크)."""
        try:
            offset = None
            updated = 0
            while True:
                results, next_offset = self.client.scroll(
                    collection_name=COLLECTION,
                    scroll_filter=Filter(
                        must=[FieldCondition(key="path", match=MatchValue(value=old_path))]
                    ),
                    limit=50,
                    offset=offset,
                    with_payload=True,
                )
                if not results:
                    break
                for point in results:
                    payload = dict(point.payload or {})
                    payload["path"] = new_path
                    self.client.set_payload(
                        collection_name=COLLECTION, payload=payload, points=[point.id]
                    )
                    updated += 1
                if next_offset is None:
                    break
                offset = next_offset
            if updated:
                logger.debug(f"Qdrant path 갱신: {old_path} → {new_path} ({updated}청크)")
        except Exception as e:
            logger.warning(f"Qdrant 페이로드 갱신 실패: {e}")

    # ── Hybrid 검색 ──────────────────────────────────────

    def _hybrid_search(
        self, text: str, limit: int = 20, exclude_paths: Optional[List[str]] = None
    ) -> List[dict]:
        dense_vec = self.llm.embed(text)
        if not dense_vec:
            return []
        sparse_idx, sparse_val = self._sparse_encode(text)

        filter_condition = None
        if exclude_paths:
            filter_condition = Filter(
                must_not=[
                    FieldCondition(key="path", match=MatchValue(value=p))
                    for p in exclude_paths
                ]
            )

        try:
            results = self.client.query_points(
                collection_name=COLLECTION,
                prefetch=[
                    Prefetch(query=dense_vec, using="dense", limit=limit * 2),
                    Prefetch(
                        query=SparseVector(indices=sparse_idx, values=sparse_val),
                        using="sparse",
                        limit=limit * 2,
                    ),
                ],
                query=FusionQuery(fusion=Fusion.RRF),
                query_filter=filter_condition,
                limit=limit,
                with_payload=True,
            )
            hits = getattr(results, "points", results)
            out = []
            for r in hits:
                p = r.payload or {}
                out.append(
                    {
                        "path": p.get("path", ""),
                        "title": p.get("title", ""),
                        "domain": p.get("domain", ""),
                        "content": p.get("content", ""),
                        "chunk_idx": p.get("chunk_idx", 0),
                        "note_id": p.get("note_id", ""),
                        "score": getattr(r, "score", 0.0),
                    }
                )
            return out
        except Exception as e:
            logger.warning(f"Hybrid search 실패, dense 폴백: {e}")
            return self._dense_search(dense_vec, limit, exclude_paths)

    def _dense_search(
        self,
        dense_vec: List[float],
        limit: int,
        exclude_paths: Optional[List[str]] = None,
    ) -> List[dict]:
        filter_condition = None
        if exclude_paths:
            filter_condition = Filter(
                must_not=[
                    FieldCondition(key="path", match=MatchValue(value=p))
                    for p in exclude_paths
                ]
            )
        results = self.client.search(
            collection_name=COLLECTION,
            query_vector=("dense", dense_vec),
            query_filter=filter_condition,
            limit=limit,
        )
        out = []
        for r in results:
            p = r.payload or {}
            out.append(
                {
                    "path": p.get("path", ""),
                    "title": p.get("title", ""),
                    "domain": p.get("domain", ""),
                    "content": p.get("content", ""),
                    "chunk_idx": p.get("chunk_idx", 0),
                    "note_id": p.get("note_id", ""),
                    "score": r.score,
                }
            )
        return out

    def find_similar(
        self, content: str, threshold: float, exclude_paths: Optional[List[str]] = None
    ) -> List[dict]:
        hits = self._hybrid_search(content, limit=20, exclude_paths=exclude_paths)
        return [h for h in hits if h["score"] >= threshold]

    def check_duplicate(self, content: str, threshold: float = 0.90) -> Optional[dict]:
        """threshold 이상의 유사 노트가 있으면 반환, 없으면 None."""
        hits = self._hybrid_search(content, limit=3)
        if hits and hits[0]["score"] >= threshold:
            return hits[0]
        return None

    # ── [[Wiki Link]] 삽입 ───────────────────────────────

    _MAX_RELATED_LINKS = 20

    def insert_links_into_note(self, note_path: Path, suggested_links: List[str]) -> None:
        if not suggested_links or not note_path.exists():
            return
        content = note_path.read_text(encoding="utf-8")
        existing_links = set(re.findall(r"\[\[(.+?)\]\]", content))
        link_section_marker = "## 연관 노트"
        if link_section_marker in content:
            sec_start = content.index(link_section_marker)
            existing_count = len(re.findall(r"\[\[", content[sec_start:]))
            if existing_count >= self._MAX_RELATED_LINKS:
                return
            slots = self._MAX_RELATED_LINKS - existing_count
        else:
            slots = self._MAX_RELATED_LINKS
        new_links = [l for l in suggested_links if l not in existing_links][:slots]
        if not new_links:
            return
        link_block = "\n".join(f"[[{l}]]" for l in new_links)
        if link_section_marker in content:
            content = content.replace(link_section_marker, f"{link_section_marker}\n{link_block}", 1)
        else:
            content = content.rstrip() + f"\n\n---\n\n## 연관 노트\n{link_block}\n"
        content = re.sub(r'\n{3,}', '\n\n', content)
        content = re.sub(r'^- unknown\s*$\n?', '', content, flags=re.MULTILINE | re.IGNORECASE)
        content = re.sub(r'^unknown\s*$\n?', '', content, flags=re.MULTILINE | re.IGNORECASE)
        note_path.write_text(content, encoding="utf-8")

    # ── 백링크 ───────────────────────────────────────────

    def add_backlink(self, target_note_path: Path, source_title: str) -> None:
        if not target_note_path.exists():
            return
        content = target_note_path.read_text(encoding="utf-8")
        if f"[[{source_title}]]" in content:
            return
        marker = "## 연관 노트"
        if marker in content:
            sec_start = content.index(marker)
            existing_count = len(re.findall(r"\[\[", content[sec_start:]))
            if existing_count >= self._MAX_RELATED_LINKS:
                return
        backlink_block = f"[[{source_title}]]"
        if marker in content:
            content = content.replace(marker, f"{marker}\n{backlink_block}", 1)
        else:
            content = content.rstrip() + f"\n\n---\n\n## 연관 노트\n{backlink_block}\n"
        content = re.sub(r'\n{3,}', '\n\n', content)
        content = re.sub(r'^- unknown\s*$\n?', '', content, flags=re.MULTILINE | re.IGNORECASE)
        content = re.sub(r'^unknown\s*$\n?', '', content, flags=re.MULTILINE | re.IGNORECASE)
        target_note_path.write_text(content, encoding="utf-8")

    # ── 전체 링크 파이프라인 ─────────────────────────────

    def link_note(
        self,
        note_path: Path,
        note_title: str,
        note_domain: str,
        note_content: str,
        all_notes: List[dict],
    ) -> List[str]:
        llm_links = self.llm.suggest_links(note_content, all_notes)
        # Qwen 언로드 → 240s Metal 회수 대기 (nomic 로드 전 macOS가 9.9GB 완전 반환)
        # find_similar 제거: nomic 로드를 link_note에서 완전 배제 → precompute까지 nomic 미사용
        # M3 Pro 18GB: Qwen 언로드 직후 nomic 로드 시 Docker Desktop이 SIGKILL → 240s 대기 필수
        self.llm.unload_model()
        all_titles = {n["title"] for n in all_notes}
        llm_links = [l for l in llm_links if l in all_titles and l != note_title]
        combined = list(dict.fromkeys(llm_links))
        resolved = list(dict.fromkeys(
            self._resolve_part_title(t, all_notes) for t in combined
        ))
        self.insert_links_into_note(note_path, resolved)
        source_stem = Path(note_path).stem
        for title in combined:
            target = self._find_note_path_by_title(title, all_notes)
            if target:
                self.add_backlink(Path(target), source_stem)
        return resolved

    def _find_note_path_by_title(self, title: str, all_notes: List[dict]) -> Optional[str]:
        for note in all_notes:
            if note.get("title") == title:
                return note.get("path")
        return None

    def _resolve_part_title(self, title: str, all_notes: List[dict]) -> str:
        """Base title → actual file stem. For Part-split notes returns 'Title Part1' stem."""
        for note in all_notes:
            if note.get("title") == title:
                stem = Path(note.get("path", "")).stem
                if stem and stem != title:
                    return stem
        return title

    def _find_note_path_by_stem(self, stem: str, all_notes: List[dict]) -> Optional[str]:
        """Find note path by file stem, falling back to title match."""
        for note in all_notes:
            if Path(note.get("path", "")).stem == stem:
                return note.get("path")
            if note.get("title") == stem:
                return note.get("path")
        return None

    # ── 전체 노트 재링크 ─────────────────────────────────

    def relink_all_notes(self, db, qdrant_only: bool = True) -> int:
        all_notes = db.get_all_notes()
        if not all_notes:
            return 0
        mode = "Qdrant전용" if qdrant_only else "LLM+Qdrant"
        logger.info(f"전체 노트 재링크 시작: {len(all_notes)}개 노트 [{mode}]")
        updated = 0
        for note in all_notes:
            path = Path(note["path"])
            if not path.exists():
                logger.debug(f"재링크 건너뜀 (파일 없음): {note['path']}")
                continue
            try:
                content = path.read_text(encoding="utf-8")
                if qdrant_only:
                    # RRF fusion 점수 범위는 0~0.5 (cosine similarity 아님)
                    # config.similarity_threshold(0.85)는 cosine용이므로 RRF에는 0.3 사용
                    similar = self.find_similar(
                        content,
                        threshold=0.3,
                        exclude_paths=[str(path)],
                    )
                    link_titles = [s["title"] for s in similar[:10] if s["title"] != note["title"]]
                    if link_titles:
                        resolved = list(dict.fromkeys(
                            self._resolve_part_title(t, all_notes) for t in link_titles
                        ))
                        self.insert_links_into_note(path, resolved)
                        source_name = Path(note["path"]).stem
                        for rt in resolved:
                            target = self._find_note_path_by_stem(rt, all_notes)
                            if target:
                                self.add_backlink(Path(target), source_name)
                        link_titles = resolved
                else:
                    link_titles = self.link_note(path, note["title"], note["domain"], content, all_notes)
                if link_titles:
                    updated += 1
                    logger.info(f"재링크: {note['title']} → {len(link_titles)}개 링크")
            except Exception as e:
                logger.warning(f"재링크 실패 [{note.get('title', '')}]: {e}")
        logger.info(f"전체 노트 재링크 완료: {updated}/{len(all_notes)}개 업데이트")
        sanitized = self.sanitize_all_notes(all_notes)
        if sanitized:
            logger.info(f"노트 정제 완료: {sanitized}개 파일 (빈줄/unknown 정리)")
        return updated

    def sanitize_all_notes(self, all_notes: list) -> int:
        """모든 노트의 빈줄3+ / unknown 항목 일괄 정리."""
        fixed = 0
        for note in all_notes:
            path = Path(note["path"])
            if not path.exists():
                continue
            try:
                content = path.read_text(encoding="utf-8")
                cleaned = content
                cleaned = re.sub(r'\[\[\[+', '[[', cleaned)
                cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
                cleaned = re.sub(r'^- unknown\s*$\n?', '', cleaned, flags=re.MULTILINE | re.IGNORECASE)
                cleaned = re.sub(r'^unknown\s*$\n?', '', cleaned, flags=re.MULTILINE | re.IGNORECASE)
                if cleaned != content:
                    path.write_text(cleaned, encoding="utf-8")
                    fixed += 1
            except Exception as e:
                logger.debug(f"sanitize 건너뜀 [{note.get('title', '')}]: {e}")
        return fixed

    # ── 누락 색인 복구 ────────────────────────────────────

    @staticmethod
    def _reindex_log(msg: str) -> None:
        """SIGKILL 생존 파일 로그 — stdout 파이프 버퍼 소실 방지."""
        import datetime
        import sys
        import os
        ts = datetime.datetime.now().isoformat()
        line = f"{ts} {msg}\n"
        try:
            sys.stderr.write(f"[REINDEX] {line}")
            sys.stderr.flush()
        except Exception:
            pass
        try:
            with open("/app/src/reindex_debug.log", "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            pass

    def reindex_missing(self, db, limit: int = None, embed_in_subprocess: bool = False) -> int:
        self._reindex_log("=== reindex_missing 시작 ===")
        notes = db.get_notes_without_qdrant()
        self._reindex_log(f"DB 조회 완료: {len(notes)}개 누락")
        if not notes:
            return 0
        if limit:
            notes = notes[:limit]

        # 1단계: 파일 읽기 및 청크 생성
        self._reindex_log(f"대상 {len(notes)}개 — 파일읽기 시작")
        note_data = []
        for i, note in enumerate(notes):
            path = Path(note["path"])
            self._reindex_log(f"[{i+1}/{len(notes)}] 경로확인: {path}")
            if not path.exists():
                logger.warning(f"재색인 건너뜀 (파일 없음): {note['path']}")
                self._reindex_log(f"[{i+1}/{len(notes)}] 파일 없음: {note['path']}")
                continue
            self._reindex_log(f"[{i+1}/{len(notes)}] read_text 시작")
            content = path.read_text(encoding="utf-8")
            self._reindex_log(f"[{i+1}/{len(notes)}] read_text 완료 ({len(content)}bytes)")
            chunks = self._chunk_text(content)
            note_data.append({
                "note": note,
                "path": path,
                "chunks": chunks,
                "note_id": str(uuid.uuid4()),
            })
            self._reindex_log(f"[{i+1}/{len(notes)}] 청크완료: {note.get('title','?')} ({len(chunks)}청크)")

        if not note_data:
            return 0

        # 2단계: dense embed
        all_chunks = [c for nd in note_data for c in nd["chunks"]]
        chunk_counts = [len(nd["chunks"]) for nd in note_data]
        self._reindex_log(f"dense embed 시작: {len(all_chunks)}청크 / {len(note_data)}노트 (in_subprocess={embed_in_subprocess})")
        try:
            if embed_in_subprocess:
                import multiprocessing
                from concurrent.futures import ProcessPoolExecutor
                ctx = multiprocessing.get_context('spawn')
                with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as executor:
                    future = executor.submit(
                        _dense_embed_subprocess,
                        "/root/.cache/fastembed",
                        all_chunks,
                    )
                    all_dense_vecs = future.result(timeout=600)
            else:
                model = self.llm._get_fastembed_model()
                all_dense_vecs = [list(v) for v in model.embed(all_chunks, batch_size=32)]
            self._reindex_log(f"dense embed 완료: {len(all_dense_vecs)}벡터")
        except Exception as e:
            logger.error(f"dense embed 실패: {e}")
            self._reindex_log(f"dense embed 실패: {e}")
            return 0

        # 3단계: sparse encode + Qdrant upsert + DB 저장 (메인 프로세스)
        vec_idx = 0
        count = 0
        for idx, (nd, chunk_cnt) in enumerate(zip(note_data, chunk_counts)):
            dense_vecs = all_dense_vecs[vec_idx:vec_idx + chunk_cnt]
            vec_idx += chunk_cnt
            note = nd["note"]
            try:
                points = []
                for j, (chunk_text, dense_vec) in enumerate(zip(nd["chunks"], dense_vecs)):
                    sparse_idx, sparse_val = self._sparse_encode(chunk_text)
                    points.append(PointStruct(
                        id=str(uuid.uuid4()),
                        vector={
                            "dense": dense_vec,
                            "sparse": SparseVector(indices=sparse_idx, values=sparse_val),
                        },
                        payload={
                            "path": str(nd["path"]),
                            "title": note["title"],
                            "domain": note["domain"],
                            "content": chunk_text,
                            "chunk_idx": j,
                            "total_chunks": chunk_cnt,
                            "note_id": nd["note_id"],
                        },
                    ))
                if points:
                    self.client.upsert(collection_name=COLLECTION, points=points)
                db.upsert_note(
                    note_path=note["path"],
                    title=note["title"],
                    domain=note["domain"],
                    tags=[],
                    source_file="",
                    qdrant_id=nd["note_id"],
                )
                count += 1
                logger.info(f"재색인 완료: {note['title']}")
                self._reindex_log(f"[{idx+1}/{len(note_data)}] 저장완료: {note.get('title','?')}")
            except Exception as e:
                logger.error(f"재색인 실패 [{note['title']}]: {e}")
                self._reindex_log(f"[{idx+1}/{len(note_data)}] 저장실패: {note.get('title','?')}: {e}")

        logger.info(f"Qdrant 재색인 완료: {count}/{len(note_data)}개 복구")
        self._reindex_log(f"=== reindex_missing 완료: {count}/{len(note_data)} ===")
        return count
