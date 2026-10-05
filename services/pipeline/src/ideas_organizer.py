"""
02_Ideas 폴더 자율 재편성
- Qdrant 임베딩 기반 유사 폴더 탐지
- LLM 이중 검증 후 병합 (보수적: 의심스러우면 유지)
- 파일 이동 + DB 경로 갱신
"""
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import Config
from .db import Database
from .llm import LLMClient

logger = logging.getLogger(__name__)


class IdeasOrganizer:
    def __init__(self, config: Config, db: Database, llm: LLMClient, linker=None):
        self.ideas_path = config.vault_path / "02_Ideas"
        self.config = config
        self.db = db
        self.llm = llm
        self.linker = linker

    def reorganize(self) -> None:
        """유사 폴더 탐지 → LLM 검증 → 병합"""
        if not self.ideas_path.exists():
            return

        folders = [f for f in self.ideas_path.iterdir() if f.is_dir()]
        if len(folders) < 2:
            return

        logger.info(f"Ideas 재편성 시작: {len(folders)}개 폴더")

        folder_embeds = self._compute_embeddings(folders)
        if len(folder_embeds) < 2:
            return

        candidates = self._find_candidates(folder_embeds)
        if not candidates:
            logger.info("Ideas 재편성: 병합 후보 없음")
            return

        merged: set = set()
        for folder_a, folder_b, sim in candidates:
            if folder_a in merged or folder_b in merged:
                continue

            decision = self.llm.evaluate_folder_merge(
                folder_a.name, self._sample_notes(folder_a),
                folder_b.name, self._sample_notes(folder_b),
            )

            if decision.get("should_merge"):
                new_name = decision.get("merged_name", "").strip()
                if not new_name:
                    new_name = folder_a.name
                self._merge_folders(folder_a, folder_b, new_name)
                merged.add(folder_a)
                merged.add(folder_b)
                logger.info(
                    f"Ideas 병합: [{folder_a.name}] + [{folder_b.name}] → [{new_name}] "
                    f"(유사도 {sim:.3f}, 이유: {decision.get('reason', '')})"
                )
            else:
                logger.debug(
                    f"Ideas 병합 보류: [{folder_a.name}] + [{folder_b.name}] "
                    f"(유사도 {sim:.3f}, 이유: {decision.get('reason', '')})"
                )

        logger.info("Ideas 재편성 완료")

    # ── 임베딩 ────────────────────────────────────────────

    def _compute_embeddings(self, folders: List[Path]) -> Dict[Path, List[float]]:
        result: Dict[Path, List[float]] = {}
        min_notes = self.config.ideas_min_notes_to_merge

        for folder in folders:
            notes = list(folder.glob("*.md"))
            if len(notes) < min_notes:
                continue
            embed = self._avg_embedding(notes)
            if embed:
                result[folder] = embed

        return result

    def _avg_embedding(self, notes: List[Path]) -> Optional[List[float]]:
        embeddings: List[List[float]] = []
        for note in notes[:10]:
            try:
                text = note.read_text(encoding="utf-8")[:2000]
                embed = self.llm.embed(text)
                if embed:
                    embeddings.append(embed)
            except Exception:
                continue

        if not embeddings:
            return None

        dim = len(embeddings[0])
        avg = [sum(e[i] for e in embeddings) / len(embeddings) for i in range(dim)]
        return avg

    # ── 후보 탐지 ─────────────────────────────────────────

    def _find_candidates(
        self, folder_embeds: Dict[Path, List[float]]
    ) -> List[Tuple[Path, Path, float]]:
        threshold = self.config.ideas_merge_threshold
        folders = list(folder_embeds.keys())
        candidates: List[Tuple[Path, Path, float]] = []

        for i in range(len(folders)):
            for j in range(i + 1, len(folders)):
                sim = _cosine_similarity(folder_embeds[folders[i]], folder_embeds[folders[j]])
                if sim >= threshold:
                    candidates.append((folders[i], folders[j], sim))

        candidates.sort(key=lambda x: x[2], reverse=True)
        return candidates

    # ── 병합 실행 ─────────────────────────────────────────

    def _merge_folders(self, folder_a: Path, folder_b: Path, new_name: str) -> None:
        target = self.ideas_path / new_name
        target.mkdir(parents=True, exist_ok=True)

        for src_folder in [folder_a, folder_b]:
            for md_file in src_folder.glob("*.md"):
                dest = _unique_dest(target, md_file.name)
                self.db.update_note_path(str(md_file), str(dest))
                if self.linker:
                    self.linker.update_point_payload(str(md_file), str(dest))
                md_file.rename(dest)

            # 빈 폴더 제거
            remaining = list(src_folder.iterdir())
            if not remaining:
                src_folder.rmdir()
            else:
                logger.warning(f"Ideas 폴더 비우기 실패 (비MD 파일 존재): {src_folder}")

    def _sample_notes(self, folder: Path, max_chars: int = 1500) -> str:
        notes = sorted(folder.glob("*.md"))[:3]
        if not notes:
            return "(노트 없음)"
        per = max_chars // max(len(notes), 1)
        parts = []
        for n in notes:
            try:
                parts.append(n.read_text(encoding="utf-8")[:per])
            except Exception:
                pass
        return "\n\n---\n\n".join(parts)


# ── 유틸 ─────────────────────────────────────────────────

def _cosine_similarity(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _unique_dest(target_dir: Path, filename: str) -> Path:
    dest = target_dir / filename
    if not dest.exists():
        return dest
    stem, suffix = Path(filename).stem, Path(filename).suffix
    counter = 1
    while dest.exists():
        dest = target_dir / f"{stem}_{counter}{suffix}"
        counter += 1
    return dest
