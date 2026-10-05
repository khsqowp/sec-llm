"""
Perplexity sonar-pro API 클라이언트
URL 출처 반환 + 선택적 URL 본문 fetch
"""
import logging
import re
import requests
from typing import Dict, List

logger = logging.getLogger(__name__)

_HEADERS_TEMPLATE = {
    "Content-Type": "application/json",
}


class PerplexityClient:
    def __init__(self, api_key: str, model: str = "sonar-pro"):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://api.perplexity.ai/chat/completions"

    def query(self, question: str) -> Dict:
        """
        Perplexity에 질문하고 {answer, urls} 반환.
        answer: 짧은 팩트 요약 (150~200 토큰)
        urls: 출처 URL 목록
        """
        if not self.api_key:
            logger.warning("PERPLEXITY_API_KEY 미설정, 건너뜀")
            return {"answer": "", "urls": []}

        headers = {**_HEADERS_TEMPLATE, "Authorization": f"Bearer {self.api_key}"}
        payload = {
            "model": self.model,
            "max_tokens": 300,
            "return_citations": True,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a factual cybersecurity assistant. "
                        "Provide a concise 2-3 sentence factual answer. "
                        "Do not speculate. Cite sources."
                    ),
                },
                {"role": "user", "content": question},
            ],
        }

        try:
            resp = requests.post(self.base_url, json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            answer = ""
            urls: List[str] = []

            choices = data.get("choices", [])
            if choices:
                answer = choices[0].get("message", {}).get("content", "")

            citations = data.get("citations", [])
            if citations:
                urls = [c if isinstance(c, str) else c.get("url", "") for c in citations]
                urls = [u for u in urls if u]

            return {"answer": answer, "urls": urls[:5]}

        except requests.RequestException as e:
            logger.error(f"Perplexity API 오류: {e}")
            return {"answer": "", "urls": []}

    def fetch_url_content(self, url: str, max_chars: int = 3000) -> str:
        """URL에서 본문 텍스트 추출 (HTML 태그 제거)"""
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; DolphinAI/1.0)"},
                timeout=15,
            )
            resp.raise_for_status()
            html = resp.text

            html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"\s+", " ", text).strip()

            return text[:max_chars]
        except Exception as e:
            logger.warning(f"URL fetch 실패 [{url}]: {e}")
            return ""
