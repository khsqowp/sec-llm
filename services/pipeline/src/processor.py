"""
파일/폴더 처리 오케스트레이션
파싱 → 도메인 분류 → 마크다운 생성 → 링크 → Storage 이동 → DB 기록 → 활동 로그
"""
import hashlib
import logging
import re
import time
from pathlib import Path
from typing import List, Optional, Tuple

import yaml

from .activity import ActivityLogger
from .config import Config
from .db import Database
from .linker import Linker
from .llm import LLMClient
from .moc import MocWriter
from .mover import FileMover
from .parser import parse_file
from .vault import VaultWriter

logger = logging.getLogger(__name__)


class Processor:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.llm = LLMClient(config)
        self.vault = VaultWriter(config)
        self.linker = Linker(config, self.llm)
        self.mover = FileMover(config)
        self.activity = ActivityLogger(config)
        moc_dir = config.settings.get("vault", {}).get("moc_path", "04_MOC")
        self.moc = MocWriter(config.vault_path, moc_dir)

    # ── 공개 진입점 ──────────────────────────────────────

    def process_path(self, path: Path):
        """inbox에서 감지된 파일 또는 폴더를 처리"""
        pipeline = self._detect_pipeline(path)
        if path.is_dir():
            self._process_folder(path, pipeline=pipeline)
        else:
            self._process_single_file(path, pipeline=pipeline)

    def _detect_pipeline(self, path: Path) -> str:
        """inbox/ideas/ → 'ideas', inbox/archive_localllm/ → 'security', 그 외 → 'security'"""
        try:
            rel = path.relative_to(self.config.inbox_path)
            first = rel.parts[0].lower() if rel.parts else ""
            if first == "ideas":
                return "ideas"
        except ValueError:
            pass
        return "security"

    # ── 폴더 처리 ────────────────────────────────────────

    def _process_folder(self, folder: Path, pipeline: str = "security"):
        if self.db.is_already_processed(str(folder)):
            logger.info(f"이미 처리됨 (폴더): {folder.name}")
            return

        job_id = self.db.create_job(str(folder), "folder")
        self.db.update_job(job_id, status="processing")

        all_created_notes: List[str] = []
        folder_domain = "기타"  # archive_localllm 파이프라인 기본값 (Security/기타)

        try:
            files = sorted(_collect_files(folder, self.config.skip_extensions))
            logger.info(f"폴더 처리 시작: {folder.name} ({len(files)}개 파일) [{pipeline}]")

            # 파일별 처리
            for file_path in files:
                notes = self._process_single_file(
                    file_path, parent_folder=folder, return_notes=True, pipeline=pipeline
                )
                if not notes:
                    raise RuntimeError(f"폴더 내 파일 처리 실패 또는 노트 생성 없음: {file_path.name}")
                all_created_notes.extend(notes)

            # 폴더 내 합본 생성 (유사도 기반)
            if len(all_created_notes) >= 2:
                self._attempt_folder_merge(folder.name, all_created_notes)

            # 대표 도메인 결정
            if all_created_notes:
                folder_domain = self._dominant_domain(all_created_notes)

            # 폴더 통째로 Storage 이동
            storage_dest = self.mover.move_to_storage(folder, folder_domain)

            self.db.update_job(
                job_id,
                status="completed",
                domain=folder_domain,
                created_notes=all_created_notes,
                storage_path=str(storage_dest),
            )
            self.activity.log(
                source_name=folder.name,
                domain=folder_domain,
                created_notes=all_created_notes,
                storage_path=str(storage_dest),
            )

        except Exception as e:
            logger.error(f"폴더 처리 실패 [{folder.name}]: {e}")
            self.db.update_job(job_id, status="failed", error_message=str(e))
            self.activity.log(
                source_name=folder.name,
                domain=folder_domain,
                created_notes=all_created_notes,
                storage_path="",
                status="실패",
                error=str(e),
            )

    # ── 개별 파일 처리 ───────────────────────────────────

    def _process_single_file(
        self,
        file_path: Path,
        parent_folder: Optional[Path] = None,
        return_notes: bool = False,
        pipeline: str = "security",
    ) -> Optional[List[str]]:
        ext = file_path.suffix.lower()

        if ext in self.config.skip_extensions:
            logger.debug(f"건너뜀 (스킵 확장자): {file_path.name}")
            return []

        file_hash = _md5(file_path)
        if not parent_folder and self.db.is_already_processed(str(file_path), file_hash):
            logger.info(f"이미 처리됨 (변경 없음): {file_path.name}")
            return []

        # 폴더 처리 중 재시작 시 이미 노트가 생성된 파일은 건너뜀
        if parent_folder:
            existing = self.db.get_notes_for_source(str(file_path))
            if existing:
                logger.info(f"건너뜀 (이미 처리된 파일): {file_path.name}")
                if return_notes:
                    return existing
                return None

        job_id = None
        if not parent_folder:
            job_id = self.db.create_job(str(file_path), "file", file_hash)
            self.db.update_job(job_id, status="processing")

        created_notes: List[str] = []
        domain = "기타" if pipeline == "security" else "스크랩"

        try:
            # 파싱
            text, file_type = parse_file(
                file_path,
                ollama_url=self.config.ollama_url,
                vision_model=self.config.vision_model,
            )
            if not text or text.startswith("[파싱 실패") or file_type == "unknown":
                logger.warning(f"파싱 결과 없음: {file_path.name}")
                if job_id:
                    self.db.update_job(job_id, status="failed", error_message="파싱 결과 없음 또는 지원하지 않는 형식")
                if return_notes:
                    return []
                return []

            # 중복 감지 (단독 파일 처리 시에만, 100자 이상 텍스트)
            if not parent_folder and len(text) > 100:
                dup = self.linker.check_duplicate(text, threshold=0.90)
                if dup:
                    logger.info(
                        f"중복 감지 ({dup['score']:.2f}): {file_path.name} ≈ '{dup['title']}' → 스킵"
                    )
                    storage_dest = self.mover.move_to_storage(file_path, dup.get("domain", "기타"))
                    if job_id:
                        self.db.update_job(
                            job_id,
                            status="skipped",
                            domain=dup.get("domain", "기타"),
                            created_notes=[],
                            storage_path=str(storage_dest),
                        )
                    self.activity.log(
                        source_name=file_path.name,
                        domain=dup.get("domain", "기타"),
                        created_notes=[],
                        storage_path=str(storage_dest),
                        status="스킵(중복)",
                    )
                    if return_notes:
                        return []
                    return []

            title = file_path.stem
            metadata = None

            # concept 파일 빠른 경로 — LLM 3단계 생략, 내용 그대로 보존
            if "_concept_" in file_path.stem:
                domain, markdown = _parse_concept_file(text, file_path.stem)
                logger.info(f"[concept] {file_path.name} → domain={domain} (LLM 생략)")
            else:
                toc = ""
                if pipeline == "ideas":
                    ideas_path = self.config.vault_path / "02_Ideas"
                    existing_topics = [f.name for f in ideas_path.iterdir() if f.is_dir()] if ideas_path.exists() else []
                    domain = self.llm.classify_ideas_topic(text, file_path.name, existing_topics)
                    logger.info(f"아이디어 토픽: {file_path.name} → {domain}")
                    if file_type != "code":
                        metadata = self.llm.extract_metadata(text, file_path.name)
                else:
                    if file_type != "code":
                        logger.info(f"도메인/메타데이터 추출 중: {file_path.name}")
                        domain, metadata = self.llm.classify_and_extract_metadata(text, file_path.name)
                    else:
                        domain = self.llm.classify_domain(text, file_path.name)
                    if pipeline == "security" and domain == "스크랩":
                        domain = "기타"
                    if file_path.name.startswith("PATT_"):
                        domain = "모의해킹"
                    logger.info(f"도메인 분류: {file_path.name} → {domain}")

                if file_type != "code":
                    logger.info(f"목차 추출 중: {file_path.name}")
                    toc = self.llm.extract_toc(text, file_path.name)

                markdown = self.llm.summarize_document(text, file_path.name, file_type, toc=toc)

            # 기존 노트 목록 로드 (링크용)
            all_notes = self.db.get_all_notes()

            # 기존 노트를 수정하지 않고 최초 생성 본문에 링크 포함.
            try:
                suggested_links = self.linker.suggest_note_links(title, markdown, all_notes)
            except Exception as link_err:
                logger.warning(f"링크 제안 실패 [{title}]: {link_err}")
                suggested_links = []

            # 키워드를 태그로 추출
            kw_tags = _extract_keywords(markdown) if file_type != "code" else []
            doc_tags = list(dict.fromkeys([file_type] + kw_tags))

            # 노트 저장
            note_paths = self.vault.write_individual_note(
                title=title,
                domain=domain,
                content=markdown,
                source_file=file_path.name,
                tags=doc_tags,
                links=suggested_links,
                metadata=metadata,
            )
            created_notes = [str(p) for p in note_paths]

            # 최초 생성에 링크를 포함한 노트의 note_index 저장
            # embed/Qdrant 인덱싱 완전 제거 — nomic+Qwen 동시 Metal 로드가 OOM 원인
            # Qdrant 인덱싱은 모든 파일 처리 완료 후 별도 reindex 스크립트에서 실행
            for note_path in note_paths:
                # note_index 저장 (qdrant_id=NULL) — 재시작 시 중복 처리 방지
                self.db.upsert_note(
                    note_path=str(note_path),
                    title=note_path.stem,
                    domain=domain,
                    tags=doc_tags,
                    source_file=str(file_path),
                    qdrant_id=None,
                )

                # MOC 갱신
                self.moc.update(domain, note_path.stem)

            # 파일 단독 처리 시 Storage 이동
            if not parent_folder:
                storage_dest = self.mover.move_to_storage(file_path, domain)
                self.linker.update_point_payload(str(file_path), str(storage_dest))
                if job_id:
                    self.db.update_job(
                        job_id,
                        status="completed",
                        domain=domain,
                        created_notes=created_notes,
                        storage_path=str(storage_dest),
                    )
                self.activity.log(
                    source_name=file_path.name,
                    domain=domain,
                    created_notes=created_notes,
                    storage_path=str(storage_dest),
                )

        except Exception as e:
            logger.error(f"파일 처리 실패 [{file_path.name}]: {e}")
            if job_id:
                self.db.update_job(job_id, status="failed", error_message=str(e))
            if not parent_folder:
                self.activity.log(
                    source_name=file_path.name,
                    domain=domain,
                    created_notes=[],
                    storage_path="",
                    status="실패",
                    error=str(e),
                )
            return [] if return_notes else None

        if return_notes:
            return created_notes
        return None

    # ── 합본 생성 시도 ────────────────────────────────────

    def _attempt_folder_merge(self, folder_name: str, note_paths: List[str]):
        threshold = self.config.similarity_threshold
        processed_pairs: set = set()
        merge_groups: List[List[str]] = []

        for path_str in note_paths:
            path = Path(path_str)
            if not path.exists():
                continue

            content = path.read_text(encoding="utf-8")
            similar = self.linker.find_similar(
                content, threshold=threshold, exclude_paths=[path_str]
            )

            if similar:
                group_paths = [path_str] + [
                    s["path"] for s in similar if s["path"] in note_paths
                ]
                group_key = frozenset(group_paths)
                if group_key not in processed_pairs and len(group_paths) >= 2:
                    processed_pairs.add(group_key)
                    merge_groups.append(group_paths)

        for group in merge_groups:
            self._create_merged_note(group)

    def _create_merged_note(self, note_paths: List[str]):
        contents = []
        titles = []

        for path_str in note_paths:
            p = Path(path_str)
            if p.exists():
                raw = p.read_text(encoding="utf-8")
                # frontmatter 제거 후 LLM에 전달 — 이중 frontmatter 방지
                fm_end = re.search(r'^---\s*\n.*?\n---\s*\n', raw, re.DOTALL)
                body = raw[fm_end.end():] if fm_end else raw
                contents.append(body[:3000])
                titles.append(p.stem)

        if not contents:
            return

        domain_guess = self.db.get_all_notes()
        domain = "스크랩"
        for n in domain_guess:
            if n["path"] == note_paths[0]:
                domain = n["domain"]
                break

        merged_content = self.llm.create_merged_note(contents, titles)
        merged_title = " + ".join(titles[:3])
        if len(titles) > 3:
            merged_title += f" 외 {len(titles) - 3}개"

        all_notes = self.db.get_all_notes()
        try:
            links = self.linker.suggest_note_links(merged_title, merged_content, all_notes)
        except Exception as link_err:
            logger.warning(f"합본 링크 제안 실패: {link_err}")
            links = []
        merged_paths = self.vault.write_merged_note(
            title=merged_title,
            domain=domain,
            content=merged_content,
            source_titles=titles,
            links=links,
        )

        all_notes = self.db.get_all_notes()
        for mp in merged_paths:
            embed_content = mp.read_text(encoding="utf-8")
            qdrant_id = self.linker.reindex_note(mp, mp.stem, domain, embed_content)
            self.db.upsert_note(
                note_path=str(mp),
                title=mp.stem,
                domain=domain,
                tags=["합본"],
                source_file="merged",
                qdrant_id=qdrant_id,
            )


        logger.info(f"합본 노트 생성: {merged_title}")

    def _dominant_domain(self, note_paths: List[str]) -> str:
        domain_count: dict = {}
        all_notes = self.db.get_all_notes()
        note_map = {n["path"]: n["domain"] for n in all_notes}

        for p in note_paths:
            d = note_map.get(p, "기타")
            domain_count[d] = domain_count.get(d, 0) + 1

        return max(domain_count, key=domain_count.get) if domain_count else "기타"


def _md5(path: Path) -> str:
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()
    except Exception:
        return ""


def _collect_files(folder: Path, skip_extensions: List[str]) -> List[Path]:
    files = []
    for f in folder.rglob("*"):
        if f.is_file() and f.suffix.lower() not in skip_extensions:
            files.append(f)
    return files


def _parse_concept_file(text: str, stem: str) -> Tuple[str, str]:
    """frontmatter에서 domain을 읽고 본문을 그대로 반환. LLM 호출 없음."""
    domain = "기타"
    body = text

    # YAML frontmatter 파싱
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if fm_match:
        try:
            fm = yaml.safe_load(fm_match.group(1)) or {}
            domain = fm.get("domain", domain)
        except yaml.YAMLError:
            pass
        body = text[fm_match.end():]

    return domain, body


def _extract_keywords(markdown: str) -> List[str]:
    """💡 핵심 키워드 섹션에서 태그용 키워드 추출 (쉼표 또는 리스트 형식 모두 지원)"""
    match = re.search(r"##\s*💡\s*핵심\s*키워드\s*\n(.+?)(?:\n##|\Z)", markdown, re.DOTALL)
    if not match:
        return []
    block = match.group(1).strip()
    # 리스트 형식: "- 키워드" 한 줄씩
    if re.search(r"^\s*-\s+\S", block, re.MULTILINE):
        keywords = [re.sub(r"^\s*-\s+", "", l).strip() for l in block.splitlines() if re.match(r"^\s*-\s+\S", l)]
    else:
        # 쉼표 구분 형식: "키워드1, 키워드2, ..."
        keywords = [k.strip() for k in block.split(",") if k.strip()]
    # 괄호 내 영어 표기 제거: "버퍼 오버플로우(Buffer Overflow)" → "버퍼 오버플로우"
    cleaned = [re.sub(r"\s*\([^)]*\)", "", k).strip() for k in keywords]
    return [k for k in cleaned if k][:12]
