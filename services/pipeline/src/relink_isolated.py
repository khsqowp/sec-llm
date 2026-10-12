#!/usr/bin/env python3
"""
고립 노트 판별 helper. 기존 vault 노트 재링크는 create-only 정책으로 비활성화.
"""
import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger("relink")

def has_wiki_links(content: str) -> bool:
    """노트 본문에 [[링크]] 가 있는지 확인 (frontmatter 제외)."""
    # frontmatter 제거
    body = re.sub(r"^---\n.*?\n---\n", "", content, flags=re.DOTALL)
    return bool(re.search(r"\[\[.+?\]\]", body))


def main():
    logger.warning("기존 vault 노트 수정 금지: 고립 노트 재링크 비활성화")
    return 0


if __name__ == "__main__":
    main()
