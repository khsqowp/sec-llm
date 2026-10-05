"""
Claude API 클라이언트 (최후 수단 — accepted_limit 케이스 전용)
"""
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)


class ClaudeAPIClient:
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6"):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://api.anthropic.com/v1/messages"

    def review_and_fix(
        self,
        note_title: str,
        note_content: str,
        perplexity_context: str,
    ) -> Optional[str]:
        """
        보안 노트의 중대 오류를 검토하고 수정본을 반환.
        실패 시 None 반환.
        """
        if not self.api_key:
            logger.warning("ANTHROPIC_API_KEY 미설정, Claude API 건너뜀")
            return None

        system_prompt = (
            "You are a senior cybersecurity expert reviewing technical notes. "
            "You are given a note with critical errors and additional context from web sources. "
            "Your task: rewrite the note content to be factually accurate and technically complete. "
            "Preserve the original structure (headers, bullet points). "
            "Write in Korean (한국어). "
            "Do not add CVE references. "
            "Output only the corrected note content, no preamble."
        )

        user_prompt = (
            f"# Note Title: {note_title}\n\n"
            f"## Current Content (has critical errors):\n{note_content}\n\n"
            f"## Additional Context from Web Sources:\n{perplexity_context}\n\n"
            "Please rewrite the note content to fix all critical errors while preserving the structure."
        )

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": user_prompt}],
            "system": system_prompt,
        }

        try:
            resp = requests.post(self.base_url, json=payload, headers=headers, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            content = data.get("content", [])
            if content and isinstance(content, list):
                return content[0].get("text", "")
            return None
        except requests.RequestException as e:
            logger.error(f"Claude API 오류: {e}")
            return None
