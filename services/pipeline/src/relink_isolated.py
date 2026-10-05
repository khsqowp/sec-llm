#!/usr/bin/env python3
"""
링크 없는 고립 노트 재링크 스크립트
- DB에서 모든 노트를 가져와 [[링크]] 없는 노트를 식별
- Qdrant 유사도 기반으로 링크 추가 (LLM 호출 없음)
"""
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, "/app/src")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("relink")

from config import Config
from database import Database
from llm import LLMClient
from linker import Linker


def has_wiki_links(content: str) -> bool:
    """노트 본문에 [[링크]] 가 있는지 확인 (frontmatter 제외)."""
    # frontmatter 제거
    body = re.sub(r"^---\n.*?\n---\n", "", content, flags=re.DOTALL)
    return bool(re.search(r"\[\[.+?\]\]", body))


def main():
    config = Config()
    db = Database(config)
    llm = LLMClient(config)
    linker = Linker(config, llm)

    all_notes = db.get_all_notes()
    logger.info(f"전체 노트: {len(all_notes)}개")

    isolated = []
    for note in all_notes:
        path = Path(note["path"])
        if not path.exists():
            continue
        try:
            content = path.read_text(encoding="utf-8")
            if not has_wiki_links(content):
                isolated.append(note)
        except Exception as e:
            logger.warning(f"파일 읽기 실패: {note['path']}: {e}")

    logger.info(f"링크 없는 고립 노트: {len(isolated)}개")

    if not isolated:
        logger.info("재링크할 노트 없음.")
        return

    # Qdrant 유사도 기반 재링크
    updated = 0
    threshold = config.similarity_threshold - 0.1

    for i, note in enumerate(isolated, 1):
        path = Path(note["path"])
        try:
            content = path.read_text(encoding="utf-8")
            similar = linker.find_similar(
                content,
                threshold=threshold,
                exclude_paths=[str(path)],
            )
            link_titles = [
                s["title"] for s in similar[:10]
                if s["title"] != note["title"]
            ]
            if not link_titles:
                logger.info(f"[{i}/{len(isolated)}] 유사 노트 없음: {note['title'][:50]}")
                continue

            resolved = list(dict.fromkeys(
                linker._resolve_part_title(t, all_notes) for t in link_titles
            ))
            linker.insert_links_into_note(path, resolved)
            source_stem = Path(note["path"]).stem
            for rt in resolved:
                target = linker._find_note_path_by_stem(rt, all_notes)
                if target:
                    linker.add_backlink(Path(target), source_stem)

            updated += 1
            logger.info(f"[{i}/{len(isolated)}] 재링크 완료: {note['title'][:50]} → {len(resolved)}개 링크")

        except Exception as e:
            logger.warning(f"[{i}/{len(isolated)}] 재링크 실패: {note.get('title', '?')[:50]}: {e}")

    logger.info(f"재링크 완료: {updated}/{len(isolated)}개 업데이트")


if __name__ == "__main__":
    main()
