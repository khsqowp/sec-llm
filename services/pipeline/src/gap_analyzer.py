"""
Gap Analyzer — 도메인별 지식 공백 감지 및 개념서 자동 생성
매일 자정 Health Check에서만 실행 (post_process_check 제외)

흐름:
1. 노트 수 적은 도메인 우선 선정 (domains_per_run개)
2. 도메인별 기존 노트 제목 → Qwen: 빠진 개념 식별
3. 중복 체크 (gap_log) + 일일 한도 체크
4. Perplexity 컨텍스트 수집 (선택)
5. Qwen: 개념서 초안 작성 → 00_inbox 저장
6. gap_log 기록
"""
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .config import Config
from .new_files import write_new_file
from .db import Database
from .llm import LLMClient
from .perplexity import PerplexityClient

logger = logging.getLogger(__name__)

# gap 분석 대상에서 제외할 도메인 (아이디어/스크랩 등 비기술 도메인)
_SKIP_DOMAINS = {"아이디어", "스크랩", "기타", "미분류"}


class GapAnalyzer:
    def __init__(self, config: Config, db: Database, llm: LLMClient):
        self.config = config
        self.db = db
        self.llm = llm
        self.inbox = config.inbox_path
        self.perplexity = PerplexityClient(
            api_key=config.perplexity_api_key,
            model=config.validation_perplexity_model,
        )

    def run(self) -> dict:
        """Gap 분석 실행. Health Check 스케줄러에서 호출."""
        if not self.config.gap_analysis_enabled:
            return {"skipped": True}

        daily_limit = self.config.gap_analysis_daily_limit
        today_count = self.db.get_today_gap_count()
        if today_count >= daily_limit:
            logger.info(f"Gap 분석 일일 한도 도달 ({today_count}/{daily_limit}) — 건너뜀")
            return {"skipped_daily_limit": True, "today_count": today_count}

        remaining = daily_limit - today_count
        domains = self._select_domains()
        if not domains:
            logger.info("분석할 도메인 없음")
            return {"generated": 0}

        logger.info(f"Gap 분석 시작: 도메인 {domains} (오늘 {today_count}/{daily_limit})")

        total_generated = 0
        results = {}

        for domain in domains:
            if total_generated >= remaining:
                break
            generated = self._analyze_domain(domain, remaining - total_generated)
            results[domain] = generated
            total_generated += generated

        logger.info(f"Gap 분석 완료: 총 {total_generated}개 개념서 생성 — {results}")
        return {"generated": total_generated, "by_domain": results}

    def _select_domains(self) -> List[str]:
        """노트 수 적은 순으로 domains_per_run개 도메인 선택"""
        counts = self.db.get_domain_note_counts()
        eligible = {d: c for d, c in counts.items() if d not in _SKIP_DOMAINS and c > 0}
        sorted_domains = sorted(eligible, key=lambda d: eligible[d])
        return sorted_domains[: self.config.gap_analysis_domains_per_run]

    def _analyze_domain(self, domain: str, budget: int) -> int:
        """단일 도메인 gap 분석 및 개념서 생성. budget = 남은 일일 한도."""
        existing_titles = self.db.get_domain_note_titles(domain)
        already_generated = self.db.get_generated_gap_topics(domain)

        gap_topics = self.llm.identify_gaps(domain, existing_titles, already_generated)
        if not gap_topics:
            logger.info(f"[{domain}] gap 주제 없음")
            return 0

        max_per_domain = min(
            self.config.gap_analysis_gaps_per_domain,
            budget,
            len(gap_topics),
        )
        logger.info(f"[{domain}] gap 주제 {len(gap_topics)}개 식별 → 최대 {max_per_domain}개 생성")

        generated = 0
        for topic in gap_topics[:max_per_domain]:
            if topic in already_generated:
                continue
            try:
                output_path = self._generate_concept(topic, domain)
                if output_path:
                    self.db.log_gap(domain, topic, str(output_path))
                    generated += 1
                    already_generated.append(topic)
            except Exception as e:
                logger.error(f"[{domain}] '{topic}' 개념서 생성 실패: {e}")

        return generated

    def _generate_concept(self, topic: str, domain: str) -> Optional[Path]:
        """Perplexity 컨텍스트 수집 + Qwen 개념서 초안 작성 → inbox 저장"""
        context = ""
        if self.config.gap_analysis_perplexity_enabled and self.config.perplexity_api_key:
            context = self._fetch_perplexity_context(topic, domain)

        content = self.llm.generate_concept_draft(topic, domain, context)
        if not content.strip():
            logger.warning(f"[{domain}] '{topic}' — LLM이 빈 내용 반환")
            return None

        # front matter 삽입 후 inbox 저장
        full_content = self._wrap_with_frontmatter(topic, domain, content)
        return self._write_to_inbox(topic, full_content)

    def _fetch_perplexity_context(self, topic: str, domain: str) -> str:
        """Perplexity로 개념서 작성용 컨텍스트 수집"""
        query = f"{domain} {topic} 기술 개요 사이버보안"
        try:
            result = self.perplexity.query(query)
            answer = result.get("answer", "")
            urls = result.get("urls", [])
            parts = []
            if answer:
                parts.append(f"[검색: {query}]\n{answer}")
            if urls:
                url_content = self.perplexity.fetch_url_content(urls[0], max_chars=1500)
                if url_content:
                    parts.append(f"[출처: {urls[0]}]\n{url_content}")
            return "\n\n---\n\n".join(parts)
        except Exception as e:
            logger.warning(f"Perplexity 컨텍스트 수집 실패 [{topic}]: {e}")
            return ""

    @staticmethod
    def _wrap_with_frontmatter(topic: str, domain: str, content: str) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        return (
            f"---\n"
            f"title: \"{topic}\"\n"
            f"domain: \"{domain}\"\n"
            f"source: gap_analyzer\n"
            f"created: {today}\n"
            f"---\n\n"
            f"# {topic}\n\n"
            f"{content}"
        )

    def _write_to_inbox(self, topic: str, content: str) -> Optional[Path]:
        try:
            safe_title = re.sub(r'[^\w\s가-힣-]', '', topic).strip()[:50]
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{safe_title}_concept_{timestamp}.md"
            output_path = self.inbox / filename
            output_path = write_new_file(output_path, content)
            logger.info(f"개념서 inbox 저장: {filename}")
            return output_path
        except Exception as e:
            logger.error(f"inbox 저장 실패 [{topic}]: {e}")
            return None
