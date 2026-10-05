"""
노트 품질 검증 파이프라인
Qwen2.5 로컬 LLM → Perplexity 보충 → Claude API (최후 수단)
"""
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from .claude_client import ClaudeAPIClient
from .config import Config
from .db import Database
from .llm import LLMClient
from .perplexity import PerplexityClient

logger = logging.getLogger(__name__)


class NoteValidator:
    def __init__(self, config: Config, db: Database, llm: LLMClient):
        self.config = config
        self.db = db
        self.llm = llm
        self.inbox = config.inbox_path
        self.perplexity = PerplexityClient(
            api_key=config.perplexity_api_key,
            model=config.validation_perplexity_model,
        )
        self.claude = ClaudeAPIClient(
            api_key=config.anthropic_api_key,
            model=config.validation_claude_model,
        )

    def validate_batch(self, limit: int = 10) -> dict:
        """Health Check 스케줄러에서 호출. limit개 노트 검증."""
        if not self.config.validation_enabled:
            return {"skipped": True}

        daily_limit = self.config.validation_daily_limit
        today_count = self.db.get_today_validation_count()
        if today_count >= daily_limit:
            logger.info(f"일일 검증 한도 도달 ({today_count}/{daily_limit}) — 건너뜀")
            return {"skipped_daily_limit": True, "today_count": today_count}

        effective_limit = min(limit, daily_limit - today_count)
        notes = self.db.get_notes_to_validate(limit=effective_limit)
        if not notes:
            logger.info("검증할 노트 없음")
            return {"validated": 0}

        logger.info(f"검증 시작: {len(notes)}개 노트 (오늘 {today_count}/{daily_limit})")
        results = {"good": 0, "fixed": 0, "escalated": 0, "errors": 0}

        for note_info in notes:
            path = Path(note_info["path"])
            if not path.exists():
                logger.warning(f"검증 건너뜀 (파일 없음): {note_info['path']}")
                continue
            try:
                verdict = self.validate_note(
                    note_path=path,
                    title=note_info["title"],
                    domain=note_info["domain"],
                    reprocess_count=note_info.get("reprocess_count", 0),
                )
                if verdict == "good":
                    results["good"] += 1
                elif verdict in ("insufficient_fixed", "corrected", "accepted_limit"):
                    results["fixed"] += 1
                elif verdict == "reprocess_1":
                    results["escalated"] += 1
            except Exception as e:
                logger.error(f"검증 실패 [{note_info['title']}]: {e}")
                results["errors"] += 1

        logger.info(f"검증 완료: {results}")
        return results

    def validate_note(
        self,
        note_path: Path,
        title: str,
        domain: str,
        reprocess_count: int = 0,
    ) -> str:
        """
        단일 노트 검증. 반환값: verdict 문자열
        """
        content = note_path.read_text(encoding="utf-8")

        # ── 1단계: 로컬 LLM 평가 ──────────────────────────
        eval_result = self.llm.evaluate_note(title, domain, content)
        verdict = eval_result.get("verdict", "good")
        reason = eval_result.get("reason", "")
        missing_topics = eval_result.get("missing_topics", [])

        logger.info(f"평가 결과 [{title}]: {verdict} — {reason}")

        if verdict == "good":
            self.db.mark_note_validated(str(note_path), "good")
            self.db.log_validation(str(note_path), title, "good", reason)
            return "good"

        if verdict == "insufficient_topics":
            output_path = self._handle_insufficient(content, title, domain, missing_topics)
            final_verdict = "insufficient_fixed"
            self.db.mark_note_validated(str(note_path), final_verdict)
            self.db.log_validation(
                str(note_path), title, final_verdict, reason,
                perplexity_used=bool(output_path),
                output_path=str(output_path) if output_path else "",
            )
            return final_verdict

        if verdict == "critical_error":
            return self._handle_critical(
                note_path, content, title, domain, reason, reprocess_count
            )

        # 알 수 없는 verdict는 good으로 처리
        self.db.mark_note_validated(str(note_path), "good")
        return "good"

    def _handle_insufficient(
        self,
        content: str,
        title: str,
        domain: str,
        missing_topics: list,
    ) -> Optional[Path]:
        """부족한 주제 → Perplexity로 보충 → 00_inbox에 supplement 파일 작성"""
        context = self._gather_perplexity_context(title, missing_topics)
        supplement = self.llm.create_supplement_note(title, domain, missing_topics, context)
        if not supplement.strip():
            return None
        return self._write_to_inbox(title, "supplement", supplement)

    def _handle_critical(
        self,
        note_path: Path,
        content: str,
        title: str,
        domain: str,
        reason: str,
        reprocess_count: int,
    ) -> str:
        """
        중대 오류 처리 상태 머신:
        reprocess_count=0: 로컬 LLM 수정 시도 → 자기 검증
          - still_critical=False → 수정본 inbox 저장, verdict=corrected
          - still_critical=True  → increment reprocess_count, verdict=reprocess_1
        reprocess_count=1 (accepted_limit): Claude API → 수정본 inbox 저장
        """
        if reprocess_count == 0:
            # 로컬 LLM 수정 시도
            corrected = self.llm.attempt_correction(title, domain, content, reason)
            validation = self.llm.self_validate(title, corrected)
            still_critical = validation.get("still_critical", False)
            new_reason = validation.get("reason", reason)

            if not still_critical:
                # 수정 성공
                output_path = self._write_to_inbox(title, "correction", corrected)
                self.db.mark_note_validated(str(note_path), "corrected")
                self.db.log_validation(
                    str(note_path), title, "corrected", new_reason,
                    correction_applied=True,
                    output_path=str(output_path) if output_path else "",
                )
                logger.info(f"로컬 수정 성공 [{title}]")
                return "corrected"
            else:
                # 여전히 critical → 다음 사이클에서 Claude에게 맡김
                self.db.increment_reprocess_count(str(note_path))
                self.db.log_validation(
                    str(note_path), title, "reprocess_1", new_reason,
                    correction_applied=True,
                )
                logger.info(f"로컬 수정 불충분 → reprocess_1 [{title}]")
                return "reprocess_1"

        # reprocess_count >= 1 → Claude API
        logger.info(f"Claude API 검토 시작 (accepted_limit) [{title}]")
        context = self._gather_perplexity_context(title, [], reason=reason)
        claude_result = self.claude.review_and_fix(title, content, context)

        if claude_result:
            final_note = self.llm.create_correction_note(title, domain, claude_result, reason)
            output_path = self._write_to_inbox(title, "correction_claude", final_note)
            self.db.mark_note_validated(str(note_path), "accepted_limit")
            self.db.log_validation(
                str(note_path), title, "accepted_limit", reason,
                correction_applied=True,
                perplexity_used=bool(context),
                claude_used=True,
                output_path=str(output_path) if output_path else "",
            )
            logger.info(f"Claude API 수정 완료 [{title}]")
        else:
            # Claude API도 실패 — accepted_limit으로 종료 (더 이상 재처리 없음)
            self.db.mark_note_validated(str(note_path), "accepted_limit")
            self.db.log_validation(
                str(note_path), title, "accepted_limit", reason + " [Claude API 실패]",
                claude_used=True,
            )
            logger.warning(f"Claude API 실패 — 검증 종료 [{title}]")

        return "accepted_limit"

    def _gather_perplexity_context(
        self,
        title: str,
        missing_topics: list,
        reason: str = "",
    ) -> str:
        """Perplexity로 관련 컨텍스트 수집. URL 본문 fetch 포함."""
        if not self.config.perplexity_api_key:
            return ""

        queries = self.llm.generate_perplexity_queries(title, missing_topics or [reason])
        context_parts = []

        for query in queries[: self.config.validation_max_perplexity_queries]:
            result = self.perplexity.query(query)
            answer = result.get("answer", "")
            urls = result.get("urls", [])

            if answer:
                context_parts.append(f"[검색: {query}]\n{answer}")

            # 첫 번째 URL만 전문 fetch (비용 절약)
            if urls:
                url_content = self.perplexity.fetch_url_content(urls[0], max_chars=2000)
                if url_content:
                    context_parts.append(f"[출처: {urls[0]}]\n{url_content}")

        return "\n\n---\n\n".join(context_parts)

    def _write_to_inbox(self, title: str, suffix: str, content: str) -> Optional[Path]:
        """00_inbox에 새 .md 파일 작성. 기존 파일 절대 수정하지 않음."""
        try:
            safe_title = re.sub(r'[^\w\s가-힣-]', '', title).strip()[:60]
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{safe_title}_{suffix}_{timestamp}.md"
            output_path = self.inbox / filename
            output_path.write_text(content, encoding="utf-8")
            logger.info(f"inbox 저장: {filename}")
            return output_path
        except Exception as e:
            logger.error(f"inbox 저장 실패 [{title}]: {e}")
            return None
