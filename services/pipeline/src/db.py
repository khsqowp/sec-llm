import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Float, Boolean, text
from sqlalchemy.orm import declarative_base, Session

logger = logging.getLogger(__name__)
Base = declarative_base()


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_path = Column(String(1024), nullable=False)
    source_type = Column(String(16))          # 'file' | 'folder'
    domain = Column(String(64))
    status = Column(String(16), default="pending")  # pending|processing|completed|failed|skipped
    created_notes = Column(Text)              # JSON list of note paths
    storage_path = Column(String(1024))
    error_message = Column(Text)
    file_hash = Column(String(32))            # MD5 of source file content
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)


class NoteIndex(Base):
    __tablename__ = "note_index"

    id = Column(Integer, primary_key=True, autoincrement=True)
    note_path = Column(String(1024), unique=True, nullable=False)
    title = Column(String(512))
    domain = Column(String(64))
    tags = Column(Text)                       # JSON list
    source_file = Column(String(1024))
    qdrant_id = Column(String(64))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
    validated_at = Column(DateTime, nullable=True)
    validation_verdict = Column(String(32), nullable=True)  # good|insufficient_fixed|corrected|reprocess_1|accepted_limit
    reprocess_count = Column(Integer, default=0)


class GapLog(Base):
    __tablename__ = "gap_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    domain = Column(String(64), nullable=False)
    topic = Column(String(512), nullable=False)
    output_path = Column(String(1024))
    created_at = Column(DateTime, default=datetime.utcnow)


class ValidationLog(Base):
    __tablename__ = "validation_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    note_path = Column(String(1024), nullable=False)
    note_title = Column(String(512))
    verdict = Column(String(32))
    reason = Column(Text)
    correction_applied = Column(Boolean, default=False)
    perplexity_used = Column(Boolean, default=False)
    claude_used = Column(Boolean, default=False)
    output_path = Column(String(1024))
    created_at = Column(DateTime, default=datetime.utcnow)


class Database:
    def __init__(self, postgres_url: str):
        self.engine = create_engine(postgres_url, pool_pre_ping=True)
        Base.metadata.create_all(self.engine)
        self._migrate()
        logger.info("PostgreSQL 연결 완료")

    def _migrate(self):
        with self.engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE processing_jobs ADD COLUMN IF NOT EXISTS file_hash VARCHAR(32)"
            ))
            conn.execute(text(
                "ALTER TABLE note_index ADD COLUMN IF NOT EXISTS validated_at TIMESTAMP"
            ))
            conn.execute(text(
                "ALTER TABLE note_index ADD COLUMN IF NOT EXISTS validation_verdict VARCHAR(32)"
            ))
            conn.execute(text(
                "ALTER TABLE note_index ADD COLUMN IF NOT EXISTS reprocess_count INTEGER DEFAULT 0"
            ))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS gap_log (
                    id SERIAL PRIMARY KEY,
                    domain VARCHAR(64) NOT NULL,
                    topic VARCHAR(512) NOT NULL,
                    output_path VARCHAR(1024),
                    created_at TIMESTAMP DEFAULT now()
                )
            """))
            conn.commit()

    def session(self) -> Session:
        return Session(self.engine)

    def create_job(self, source_path: str, source_type: str, file_hash: str = "") -> int:
        with self.session() as s:
            job = ProcessingJob(
                source_path=source_path,
                source_type=source_type,
                status="pending",
                file_hash=file_hash,
            )
            s.add(job)
            s.commit()
            return job.id

    def update_job(self, job_id: int, **kwargs):
        with self.session() as s:
            job = s.get(ProcessingJob, job_id)
            if not job:
                return
            for k, v in kwargs.items():
                if k == "created_notes" and isinstance(v, list):
                    v = json.dumps(v, ensure_ascii=False)
                setattr(job, k, v)
            if kwargs.get("status") in ("completed", "failed", "skipped"):
                job.completed_at = datetime.utcnow()
            s.commit()

    def is_already_processed(self, source_path: str, file_hash: str = "") -> bool:
        with self.session() as s:
            q = s.query(ProcessingJob).filter(
                ProcessingJob.source_path == source_path,
                ProcessingJob.status == "completed",
            )
            if file_hash:
                return q.filter(ProcessingJob.file_hash == file_hash).first() is not None
            return q.first() is not None

    def upsert_note(self, note_path: str, title: str, domain: str,
                    tags: List[str], source_file: str, qdrant_id: str = ""):
        with self.session() as s:
            existing = s.query(NoteIndex).filter(NoteIndex.note_path == note_path).first()
            if existing:
                existing.title = title
                existing.domain = domain
                existing.tags = json.dumps(tags, ensure_ascii=False)
                existing.source_file = source_file
                existing.qdrant_id = qdrant_id
                existing.updated_at = datetime.utcnow()
            else:
                note = NoteIndex(
                    note_path=note_path,
                    title=title,
                    domain=domain,
                    tags=json.dumps(tags, ensure_ascii=False),
                    source_file=source_file,
                    qdrant_id=qdrant_id,
                )
                s.add(note)
            s.commit()

    def update_qdrant_id(self, note_path: str, qdrant_id: str) -> None:
        """기존 노트의 색인 ID만 갱신하고 원본 메타데이터를 보존한다."""
        with self.session() as s:
            updated = s.query(NoteIndex).filter_by(note_path=note_path).update(
                {NoteIndex.qdrant_id: qdrant_id}, synchronize_session=False
            )
            if updated != 1:
                raise LookupError(f"색인 ID를 갱신할 노트가 없습니다: {note_path}")
            s.commit()

    def update_note_path(self, old_path: str, new_path: str) -> None:
        with self.session() as s:
            note = s.query(NoteIndex).filter_by(note_path=old_path).first()
            if note:
                note.note_path = new_path
                s.commit()

    def get_all_notes(self) -> List[dict]:
        with self.session() as s:
            notes = s.query(NoteIndex).all()
            return [
                {
                    "path": n.note_path,
                    "title": n.title,
                    "domain": n.domain,
                    "qdrant_id": n.qdrant_id,
                }
                for n in notes
            ]

    def get_notes_without_qdrant(self) -> List[dict]:
        """qdrant_id가 없는 노트 목록 반환 (임베딩 실패로 색인 누락된 노트)"""
        with self.session() as s:
            notes = s.query(NoteIndex).filter(
                (NoteIndex.qdrant_id == "") | (NoteIndex.qdrant_id.is_(None))
            ).all()
            return [
                {"path": n.note_path, "title": n.title, "domain": n.domain}
                for n in notes
            ]

    def get_notes_for_source(self, source_file: str) -> List[str]:
        """특정 원본 파일로부터 생성된 노트 경로 목록 반환"""
        with self.session() as s:
            notes = s.query(NoteIndex).filter(NoteIndex.source_file == source_file).all()
            return [n.note_path for n in notes]

    # ── 검증 관련 메서드 ──────────────────────────────────

    def get_notes_to_validate(self, limit: int = 10) -> List[dict]:
        """미검증 노트 또는 reprocess_count < 2인 노트를 우선순위로 반환.
        validator가 생성한 supplement/correction 파일은 제외 (루프 방지).
        """
        with self.session() as s:
            notes = (
                s.query(NoteIndex)
                .filter(
                    (NoteIndex.validated_at.is_(None)) |
                    (NoteIndex.validation_verdict == "reprocess_1")
                )
                .filter(
                    ~NoteIndex.note_path.like("%_supplement_%"),
                    ~NoteIndex.note_path.like("%_correction_%"),
                    ~NoteIndex.note_path.like("%_concept_%"),
                )
                .order_by(NoteIndex.reprocess_count.asc(), NoteIndex.created_at.asc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "path": n.note_path,
                    "title": n.title,
                    "domain": n.domain,
                    "reprocess_count": n.reprocess_count or 0,
                }
                for n in notes
            ]

    # ── Gap 분석 관련 메서드 ──────────────────────────────

    def get_generated_gap_topics(self, domain: str) -> List[str]:
        """해당 도메인에서 이미 생성된 gap 주제 목록 반환 (중복 방지용)"""
        with self.session() as s:
            logs = s.query(GapLog).filter(GapLog.domain == domain).all()
            return [g.topic for g in logs]

    def log_gap(self, domain: str, topic: str, output_path: str = "") -> None:
        with self.session() as s:
            s.add(GapLog(domain=domain, topic=topic, output_path=output_path))
            s.commit()

    def get_today_gap_count(self) -> int:
        """오늘(UTC) 생성된 gap 개념서 수"""
        from datetime import date, time
        today_start = datetime.combine(date.today(), time.min)
        with self.session() as s:
            return s.query(GapLog).filter(GapLog.created_at >= today_start).count()

    def get_domain_note_titles(self, domain: str) -> List[str]:
        """도메인별 노트 제목 목록 (gap 식별에 사용)"""
        with self.session() as s:
            notes = s.query(NoteIndex).filter(NoteIndex.domain == domain).all()
            return [n.title for n in notes if n.title]

    def get_domain_note_counts(self) -> dict:
        """도메인별 노트 수 딕셔너리 {domain: count}"""
        with self.session() as s:
            notes = s.query(NoteIndex).all()
            counts: dict = {}
            for n in notes:
                d = n.domain or "미분류"
                counts[d] = counts.get(d, 0) + 1
            return counts

    def get_today_validation_count(self) -> int:
        """오늘(UTC) validation_log에 기록된 검증 수"""
        from datetime import date, time
        today_start = datetime.combine(date.today(), time.min)
        with self.session() as s:
            return s.query(ValidationLog).filter(
                ValidationLog.created_at >= today_start
            ).count()

    def mark_note_validated(self, note_path: str, verdict: str) -> None:
        with self.session() as s:
            note = s.query(NoteIndex).filter_by(note_path=note_path).first()
            if note:
                note.validated_at = datetime.utcnow()
                note.validation_verdict = verdict
                s.commit()

    def get_reprocess_count(self, note_path: str) -> int:
        with self.session() as s:
            note = s.query(NoteIndex).filter_by(note_path=note_path).first()
            return note.reprocess_count or 0 if note else 0

    def increment_reprocess_count(self, note_path: str) -> None:
        with self.session() as s:
            note = s.query(NoteIndex).filter_by(note_path=note_path).first()
            if note:
                note.reprocess_count = (note.reprocess_count or 0) + 1
                note.validated_at = None
                note.validation_verdict = "reprocess_1"
                s.commit()

    def log_validation(
        self,
        note_path: str,
        note_title: str,
        verdict: str,
        reason: str,
        correction_applied: bool = False,
        perplexity_used: bool = False,
        claude_used: bool = False,
        output_path: str = "",
    ) -> None:
        with self.session() as s:
            log = ValidationLog(
                note_path=note_path,
                note_title=note_title,
                verdict=verdict,
                reason=reason,
                correction_applied=correction_applied,
                perplexity_used=perplexity_used,
                claude_used=claude_used,
                output_path=output_path,
            )
            s.add(log)
            s.commit()
