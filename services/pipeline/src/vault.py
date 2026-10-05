"""
Obsidian 마크다운 노트 작성 모듈
- 개별 노트 / 합본 노트 생성
- 4000단어 초과 시 Part 분할
- YAML frontmatter 삽입
"""
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from .config import Config

logger = logging.getLogger(__name__)


class VaultWriter:
    def __init__(self, config: Config):
        self.config = config

    # ── 노트 경로 결정 ────────────────────────────────────

    def note_path_for(self, domain: str, title: str) -> Path:
        folder = self.config.vault_folder_for_domain(domain)
        folder.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_filename(title)
        return folder / f"{safe_name}.md"

    # ── 개별 노트 저장 ────────────────────────────────────

    def write_individual_note(
        self,
        title: str,
        domain: str,
        content: str,
        source_file: str,
        tags: Optional[List[str]] = None,
        links: Optional[List[str]] = None,
        metadata: Optional[dict] = None,
    ) -> List[Path]:
        """
        content가 max_words_per_note 초과하면 Part1/Part2로 분할.
        생성된 파일 경로 목록 반환.
        """
        parts = self._split_into_parts(content)
        created = []

        if len(parts) == 1:
            path = self.note_path_for(domain, title)
            body = self._build_note(
                title=title,
                domain=domain,
                content=parts[0],
                source_file=source_file,
                tags=tags or [],
                links=links or [],
                part=None,
                metadata=metadata,
            )
            path.write_text(body, encoding="utf-8")
            created.append(path)
        else:
            for i, part_content in enumerate(parts, 1):
                part_title = f"{title} Part{i}"
                path = self.note_path_for(domain, part_title)
                nav_links = self._part_nav(title, i, len(parts))
                body = self._build_note(
                    title=part_title,
                    domain=domain,
                    content=part_content,
                    source_file=source_file,
                    tags=tags or [],
                    links=links or [],
                    part=(i, len(parts)),
                    nav_links=nav_links,
                    metadata=metadata,
                )
                path.write_text(body, encoding="utf-8")
                created.append(path)

        return created

    # ── 합본 노트 저장 ────────────────────────────────────

    def write_merged_note(
        self,
        title: str,
        domain: str,
        content: str,
        source_titles: List[str],
        tags: Optional[List[str]] = None,
        links: Optional[List[str]] = None,
        metadata: Optional[dict] = None,
    ) -> List[Path]:
        # Truncate merged note titles: LLM cannot reproduce long titles in [[links]]
        if len(title) > 60:
            title = title[:57] + "..."
        tags_combined = list(set((tags or []) + ["합본"]))
        source_links = [f"[[{t}]]" for t in source_titles]
        source_section = "\n## 원본 노트\n" + "\n".join(f"- {l}" for l in source_links)

        full_content = content + "\n\n" + source_section
        parts = self._split_into_parts(full_content)
        created = []

        if len(parts) == 1:
            path = self.note_path_for(domain, title)
            body = self._build_note(
                title=title,
                domain=domain,
                content=parts[0],
                source_file="합본",
                tags=tags_combined,
                links=(links or []) + source_titles,
                part=None,
                metadata=metadata,
            )
            path.write_text(body, encoding="utf-8")
            created.append(path)
        else:
            for i, part_content in enumerate(parts, 1):
                part_title = f"{title} Part{i}"
                path = self.note_path_for(domain, part_title)
                nav_links = self._part_nav(title, i, len(parts))
                body = self._build_note(
                    title=part_title,
                    domain=domain,
                    content=part_content,
                    source_file="합본",
                    tags=tags_combined,
                    links=(links or []) + source_titles,
                    part=(i, len(parts)),
                    nav_links=nav_links,
                    metadata=metadata,
                )
                path.write_text(body, encoding="utf-8")
                created.append(path)

        return created

    # ── 내부 헬퍼 ────────────────────────────────────────

    def _split_into_parts(self, content: str) -> List[str]:
        max_words = self.config.max_words_per_note
        if len(content.split()) <= max_words:
            return [content]

        # Split line-by-line to preserve newlines and markdown structure
        parts = []
        current_lines: List[str] = []
        current_word_count = 0

        for line in content.splitlines(keepends=True):
            line_words = len(line.split())
            if current_word_count + line_words > max_words and current_lines:
                parts.append("".join(current_lines))
                current_lines = [line]
                current_word_count = line_words
            else:
                current_lines.append(line)
                current_word_count += line_words

        if current_lines:
            parts.append("".join(current_lines))

        return parts

    def _build_note(
        self,
        title: str,
        domain: str,
        content: str,
        source_file: str,
        tags: List[str],
        links: List[str],
        part: Optional[Tuple[int, int]],
        nav_links: str = "",
        metadata: Optional[dict] = None,
    ) -> str:
        now = datetime.now().strftime("%Y-%m-%d")
        all_tags = [domain] + tags
        # 태그 안전화: 개행/특수문자·backtick·bold 제거 후 50자 이하로 제한
        safe_tags = [re.sub(r"`([^`]*)`", r"\1", re.sub(r"\*\*([^*]+)\*\*", r"\1", str(t).replace("\n", " ").replace("\r", ""))).strip()[:50] for t in all_tags if str(t).strip()]
        tag_yaml = "\n".join(f"  - {t}" for t in safe_tags)

        meta = metadata or {}

        def _yaml_list(items) -> str:
            if not items or items == []:
                return "[]"
            return "\n" + "\n".join(f'  - "{item}"' for item in items)

        def _val(key: str, default: str = "unknown") -> str:
            v = meta.get(key, default)
            if isinstance(v, list):
                v = v[0] if v else default
            return str(v) if v else default

        extra_fields = ""
        if meta:
            doc_date = _val("doc_date")
            version = _val("version")
            organization = _val("organization")
            doc_type = _val("doc_type")
            cves = meta.get("cves", [])
            cwes = meta.get("cwes", [])
            affected = meta.get("affected_systems", [])

            extra_fields += f"\ndoc_date: {doc_date}"
            extra_fields += f"\nversion: \"{version}\""
            extra_fields += f"\norganization: \"{organization}\""
            extra_fields += f"\ndoc_type: \"{doc_type}\""
            if cves:
                extra_fields += f"\ncves:{_yaml_list(cves)}"
            if cwes:
                extra_fields += f"\ncwes:{_yaml_list(cwes)}"
            if affected:
                extra_fields += f"\naffected_systems:{_yaml_list(affected)}"

        frontmatter = f"""---
title: "{title}"
date: {now}
domain: {domain}
source: "{source_file}"{extra_fields}
tags:
{tag_yaml}
---"""

        header = f"# {title}"
        if part:
            header += f" (Part {part[0]}/{part[1]})"

        link_section = ""
        if links:
            unique_links = list(dict.fromkeys(links))
            link_section = "\n\n---\n\n## 연관 노트\n" + "\n".join(
                f"[[{l}]]" for l in unique_links
            )

        content = re.sub(r'\[\[\[+', '[[', content)
        # 연속 3개 이상 빈 줄 → 2개로 제한 (섹션 누락 시 빈 줄 과다 방지)
        content = re.sub(r'\n{3,}', '\n\n', content)
        # "- - 텍스트" 이중 대시 버그 수정 (공백 있어야 처리 — CLI 옵션 - --flag 보존)
        content = re.sub(r'^- -\s+', r'- ', content, flags=re.MULTILINE)
        # 🔗 관련 개념 섹션의 "unknown" 단독 항목 제거
        content = re.sub(r'^unknown$\n?', '', content, flags=re.MULTILINE | re.IGNORECASE)
        content = re.sub(r'^- unknown$\n?', '', content, flags=re.MULTILINE | re.IGNORECASE)

        parts = [frontmatter, header]
        if nav_links:
            parts.append(nav_links)
        parts.append(content)
        if link_section:
            parts.append(link_section)

        return "\n\n".join(parts) + "\n"

    def _part_nav(self, base_title: str, current: int, total: int) -> str:
        nav = []
        if current > 1:
            nav.append(f"[[{base_title} Part{current - 1}|← Part{current - 1}]]")
        if current < total:
            nav.append(f"[[{base_title} Part{current + 1}|Part{current + 1} →]]")
        return "> " + " | ".join(nav) if nav else ""


def _safe_filename(title: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title)
    safe = safe.strip(". ")
    return safe[:200] if len(safe) > 200 else safe
