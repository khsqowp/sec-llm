"""
APScheduler — Brain Health Check 스케줄러
매일 자정 전체 헬스체크 + inbox 처리 완료 후 즉시 재링크/검증
"""
import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler

from .activity import ActivityLogger
from .config import Config
from .db import Database
from .gap_analyzer import GapAnalyzer
from .ideas_organizer import IdeasOrganizer
from .linker import Linker
from .llm import LLMClient
from .validator import NoteValidator

logger = logging.getLogger(__name__)


class HealthCheckScheduler:
    def __init__(self, config: Config, db: Database, llm: LLMClient, activity: ActivityLogger, pipeline_busy=None):
        self.config = config
        self.db = db
        self.llm = llm
        self.activity = activity
        self.pipeline_busy = pipeline_busy
        self._reindex_lock = threading.Lock()
        self.scheduler = BackgroundScheduler()
        self.linker = Linker(config, llm)
        self.ideas_organizer = IdeasOrganizer(config, db, llm, self.linker)
        self.validator = NoteValidator(config, db, llm)
        self.gap_analyzer = GapAnalyzer(config, db, llm)

    def start(self):
        # 매일 자정 전체 헬스체크 (vault 요약 + ideas + 재링크 + 검증)
        self.scheduler.add_job(
            self.run_health_check,
            trigger="cron",
            hour=0,
            minute=0,
            id="health_check_daily",
        )
        self.scheduler.start()
        logger.info("스케줄러 시작: 매일 자정 전체 헬스체크 + inbox 처리 완료 후 즉시 재링크/검증")

    def stop(self):
        self.scheduler.shutdown(wait=False)
        logger.info("스케줄러 종료")

    def run_health_check(self):
        if self.pipeline_busy and self.pipeline_busy.is_busy():
            logger.info("🧠 Brain Health Check 건너뜀 — 파이프라인 처리 중")
            return
        if not self._reindex_lock.acquire(blocking=False):
            logger.info("🧠 Brain Health Check 건너뜀 — 이미 reindex 실행 중")
            return

        logger.info("🧠 Brain Health Check 실행 중...")
        try:
            try:
                vault_summary = self._build_vault_summary()
                report = self.llm.health_check(vault_summary)
                self.activity.log_health_check(report)
                logger.info("Brain Health Check 완료")
            except Exception as e:
                logger.error(f"Health Check 실패: {e}")

            if self.config.ideas_reorganize_enabled:
                try:
                    self.ideas_organizer.reorganize()
                except Exception as e:
                    logger.error(f"Ideas 재편성 실패: {e}")

            try:
                recovered = self.linker.reindex_missing(self.db, limit=100)
                if recovered:
                    logger.info(f"Qdrant 누락 복구: {recovered}개")
            except Exception as e:
                logger.error(f"Qdrant 재색인 실패: {e}")
            finally:
                try:
                    self.llm.unload_fastembed()
                except Exception:
                    pass

            self.run_relink_all()

            try:
                batch_limit = self.config.settings.get("validation", {}).get("batch_size", 10)
                self.validator.validate_batch(limit=batch_limit)
            except Exception as e:
                logger.error(f"노트 검증 실패: {e}")

            try:
                self.gap_analyzer.run()
            except Exception as e:
                logger.error(f"Gap 분석 실패: {e}")
        finally:
            self._reindex_lock.release()

    def run_relink_all(self, qdrant_only: bool = True) -> int:
        """모든 노트 간 [[링크]] 재계산. 파이프라인 처리 중이면 건너뜀."""
        if self.pipeline_busy and self.pipeline_busy.is_busy():
            logger.info("재링크 건너뜀 — 파이프라인 처리 중")
            return 0
        try:
            result = subprocess.run(
                [sys.executable, "-m", "src.relink_worker"],
                timeout=7200,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                updated = int(result.stdout.strip() or "0")
                logger.info(f"전체 재링크 완료: {updated}개")
                return updated
            else:
                logger.error(f"전체 재링크 subprocess 실패 (rc={result.returncode}): {result.stderr[-300:]}")
                return 0
        except subprocess.TimeoutExpired:
            logger.error("전체 재링크 타임아웃 (7200s)")
            return 0
        except Exception as e:
            logger.error(f"전체 재링크 실패: {e}")
            return 0

    def run_post_process_check(self):
        """inbox 처리 완료 직후 자동 실행 — Qdrant 백필(우선) + 재링크 + 검증"""
        if self.pipeline_busy and self.pipeline_busy.is_busy():
            logger.info("post-process 체크 건너뜀 — 파이프라인 처리 중")
            return
        if not self._reindex_lock.acquire(blocking=False):
            logger.info("post-process 체크 건너뜀 — 이미 reindex 실행 중")
            return
        logger.info("📬 inbox 처리 완료 — Qdrant 백필 우선 실행 (재링크 전)")

        try:
            logger.info("🔄 Qdrant 백필 시작 (relink_all 전, main process 직접 실행)")
            recovered = self.linker.reindex_missing(self.db, limit=100)
            logger.info(f"✅ Qdrant 백필 완료: {recovered}개 노트 색인")
        except Exception as e:
            logger.error(f"Qdrant 백필 실패: {e}")
        finally:
            try:
                self.llm.unload_fastembed()
            except Exception:
                pass
            self._reindex_lock.release()

        self.run_relink_all()
        try:
            batch_limit = self.config.settings.get("validation", {}).get("batch_size", 10)
            self.validator.validate_batch(limit=batch_limit)
        except Exception as e:
            logger.error(f"post-process 검증 실패: {e}")

    def _build_vault_summary(self) -> str:
        all_notes = self.db.get_all_notes()
        if not all_notes:
            return "노트가 없습니다."

        domain_count: dict = {}
        for note in all_notes:
            d = note.get("domain", "미분류")
            domain_count[d] = domain_count.get(d, 0) + 1

        lines = [f"전체 노트 수: {len(all_notes)}개\n", "## 도메인별 현황"]
        for domain, count in sorted(domain_count.items()):
            lines.append(f"- {domain}: {count}개")

        lines.append("\n## 최근 노트 목록 (최대 50개)")
        for note in all_notes[-50:]:
            lines.append(f"- [[{note['title']}]] ({note.get('domain', '')})")

        return "\n".join(lines)
