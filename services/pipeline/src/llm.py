"""
Ollama API 클라이언트 — LLM 호출, 청크 분할, 도메인 분류, 마크다운 생성, 링크 제안
"""
import logging
import re
import time
from typing import Dict, List, Optional

import requests
import yaml

from .config import Config
from .parser import get_code_language

logger = logging.getLogger(__name__)


def _fmt(template: str, **kwargs) -> str:
    """str.format() 대신 리터럴 치환 — 코드 내 중괄호가 있어도 안전"""
    result = template
    for key, value in kwargs.items():
        result = result.replace(f"{{{key}}}", str(value))
    return result


def _split_by_headings(text: str, file_type: str = "doc") -> List[str]:
    """PDF [페이지 N] 마커 또는 마크다운 ## 헤딩 기준으로 분할"""
    # PDF: [페이지 N] 마커 우선
    if re.search(r'\[페이지 \d+\]', text):
        parts = re.split(r'(?=\[페이지 \d+\])', text)
        return [p.strip() for p in parts if p.strip()]
    # Markdown: ## 헤딩 기준
    if re.search(r'^## ', text, re.MULTILINE):
        parts = re.split(r'(?=^## )', text, flags=re.MULTILINE)
        return [p.strip() for p in parts if p.strip()]
    return [text]


def _merge_segments(segments: List[str], max_words: int) -> List[str]:
    """인접 세그먼트를 max_words 이하로 합산"""
    chunks: List[str] = []
    current_parts: List[str] = []
    current_words = 0

    for seg in segments:
        seg_words = len(seg.split())
        if current_words + seg_words > max_words and current_parts:
            chunks.append("\n\n".join(current_parts))
            current_parts = [seg]
            current_words = seg_words
        else:
            current_parts.append(seg)
            current_words += seg_words

    if current_parts:
        chunks.append("\n\n".join(current_parts))

    # 단일 세그먼트가 max_words를 크게 초과하면 단어 분할로 후처리
    result: List[str] = []
    for chunk in chunks:
        if len(chunk.split()) > int(max_words * 1.2):
            result.extend(_word_split(chunk, max_words))
        else:
            result.append(chunk)
    return result


def _word_split(text: str, max_words: int) -> List[str]:
    words = text.split()
    chunks: List[str] = []
    start = 0
    while start < len(words):
        chunks.append(" ".join(words[start:start + max_words]))
        start += max_words
    return chunks


class LLMClient:
    def __init__(self, config: Config):
        self.config = config
        self.base_url = config.ollama_url.rstrip("/")
        self.model = config.llm_model
        self.embedding_model = config.embedding_model
        self.timeout = config.llm_timeout
        self.max_retries = config.max_retries
        self._fastembed_lock = __import__('threading').Lock()

    # ── 내부 공통 호출 ────────────────────────────────────

    def _chat(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.3,
        num_ctx: Optional[int] = None,
    ) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "stream": False,
            "keep_alive": -1,
            "options": {
                "temperature": temperature,
                "num_ctx": num_ctx if num_ctx is not None else self.config.num_ctx_default,
            },
        }

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.post(
                    f"{self.base_url}/api/chat",
                    json=payload,
                    timeout=self.timeout,
                )
                if resp.status_code != 200:
                    logger.warning(f"Ollama 오류 바디 [{resp.status_code}]: {resp.text[:400]}")
                resp.raise_for_status()
                content = resp.json()["message"]["content"].strip()
                if not content:
                    raise ValueError("LLM 응답 내용이 비어 있습니다")
                return content
            except requests.HTTPError as e:
                wait = 30 * attempt  # HTTP 500: Ollama 내부 회복에 20-30초 필요
                logger.warning(f"LLM HTTP 오류 (시도 {attempt}/{self.max_retries}), {wait}초 후 재시도: {e}")
                if attempt < self.max_retries:
                    time.sleep(wait)
            except Exception as e:
                wait = 10 * attempt  # 연결 오류: 빠른 재시도 가능
                logger.warning(f"LLM 호출 실패 (시도 {attempt}/{self.max_retries}), {wait}초 후 재시도: {e}")
                if attempt < self.max_retries:
                    time.sleep(wait)

        raise RuntimeError(f"LLM 호출 실패: {self.max_retries}회 재시도 소진")

    def unload_fastembed(self) -> None:
        """ONNX 세션 완전 해제 (재로드 없음) — relink 완료 후 abort() 타이머 제거.
        다음 embed() 호출 시 _get_fastembed_model()이 lazy하게 재로드함."""
        with self._fastembed_lock:
            if getattr(self, '_fastembed_model', None) is not None:
                del self._fastembed_model
                self._fastembed_model = None
        import gc, ctypes
        gc.collect()
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass
        logger.info("fastembed ONNX 세션 언로드 완료 (다음 embed() 호출 시 lazy 재로드)")

    def reload_fastembed(self) -> None:
        """기존 ONNX 세션 파괴 후 재로드 — 장시간 embed 누적 후 메모리 파편화 방지."""
        with self._fastembed_lock:
            if getattr(self, '_fastembed_model', None) is not None:
                del self._fastembed_model
                self._fastembed_model = None
        import gc, ctypes
        gc.collect()
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass
        logger.info("fastembed ONNX 세션 리셋 — 재로드 중")
        self._get_fastembed_model()

    def _get_fastembed_model(self):
        # 지연 초기화: 첫 embed() 호출 시 한 번만 로드, 이후 재사용 (Lock으로 thread-safe)
        if getattr(self, '_fastembed_model', None) is not None:
            return self._fastembed_model
        with self._fastembed_lock:
            if getattr(self, '_fastembed_model', None) is not None:
                return self._fastembed_model
            import gc, os, ctypes
            # HF XET 병렬 다운로드 비활성화: 50스레드 → 단일 HTTP (스택 ~400MB 절약)
            os.environ.setdefault('HF_HUB_DISABLE_XET', '1')
            # Python 할당자가 Qwen 스트리밍 중 키운 RSS를 OS에 반환
            gc.collect()
            try:
                ctypes.CDLL("libc.so.6").malloc_trim(0)
            except Exception:
                pass
            # VM 가용 메모리 로깅
            try:
                with open("/proc/meminfo") as _f:
                    for _line in _f:
                        if _line.startswith("MemAvailable:"):
                            _kb = int(_line.split()[1])
                            logger.info(f"fastembed 로드 전 VM 가용메모리: {_kb/1024/1024:.1f}GB")
                            break
            except Exception:
                pass
            # ONNX 스레드 제한: provider_options보다 환경변수가 ARM64에서 안정적
            # intra_op_num_threads=2로 설정 시 ARM64 ONNX segfault 발생 확인 → 환경변수만 사용
            os.environ['OMP_NUM_THREADS'] = '1'
            os.environ['MKL_NUM_THREADS'] = '1'
            os.environ['OPENBLAS_NUM_THREADS'] = '1'
            os.environ['NUMEXPR_NUM_THREADS'] = '1'
            from fastembed import TextEmbedding
            self._fastembed_model = TextEmbedding(
                model_name="nomic-ai/nomic-embed-text-v1",
                cache_dir="/root/.cache/fastembed",
                providers=["CPUExecutionProvider"],
                threads=1,
            )
            logger.info(f"fastembed nomic-embed-text-v1 로드 완료 (ONNX, Docker CPU, OMP_NUM_THREADS={os.environ.get('OMP_NUM_THREADS','?')})")
        return self._fastembed_model

    def embed(self, text: str) -> Optional[List[float]]:
        # fastembed ONNX (Docker CPU 내부) — Ollama 호출 없음, Qwen Metal과 메모리 경쟁 없음
        truncated = text[:1500]
        try:
            model = self._get_fastembed_model()
            result = list(model.embed([truncated]))[0]
            return result.tolist()
        except Exception as e:
            logger.error(f"임베딩 최종 실패 — Qdrant 색인 건너뜀: {e}")
            return None

    def warmup(self) -> None:
        """모델을 GPU에 로드된 상태로 만들어 콜드 스타트 500 방지"""
        try:
            self._chat("You are ready.", "ping", temperature=0.0, num_ctx=256)
            logger.debug("모델 워밍업 완료")
        except Exception as e:
            logger.warning(f"모델 워밍업 실패 (무시): {e}")

    def _wait_for_memory_pressure(self, min_available_gb: float = 3.0, timeout_s: int = 180) -> None:
        """Docker VM /proc/meminfo 폴링: 가용 메모리가 임계값 이상이 될 때까지 대기.
        macOS balloon driver가 host Metal 해제에 반응해 Docker VM 메모리를 반환하므로
        이 값은 host Metal 압박 해소의 간접 지표다."""
        threshold_kb = int(min_available_gb * 1024 * 1024)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                with open("/proc/meminfo") as f:
                    for line in f:
                        if line.startswith("MemAvailable:"):
                            available_kb = int(line.split()[1])
                            if available_kb >= threshold_kb:
                                logger.info(f"메모리 압박 해소 확인: {available_kb/1024/1024:.1f}GB 가용")
                                return
                            logger.info(f"메모리 대기 중: {available_kb/1024/1024:.1f}GB < {min_available_gb}GB 임계값")
                            break
            except Exception:
                return  # /proc/meminfo 없음 (비Linux) → 건너뜀
            time.sleep(10)
        logger.warning(f"메모리 압박 대기 타임아웃 ({timeout_s}s) — 강제 진행")

    def unload_model(self) -> None:
        """메인 LLM을 Ollama GPU 메모리에서 내리고, 실제로 내려질 때까지 대기."""
        try:
            requests.post(
                f"{self.base_url}/api/chat",
                json={"model": self.model, "messages": [], "keep_alive": 0},
                timeout=15,
            )
            # Ollama /api/ps 폴링: Qwen이 실제로 언로드될 때까지 대기 (최대 60초)
            model_short = self.model.split(":")[0].split("/")[-1].lower()
            for _ in range(120):  # 최대 60초 대기
                time.sleep(0.5)
                try:
                    resp = requests.get(f"{self.base_url}/api/ps", timeout=5)
                    if resp.ok:
                        running = [m.get("name", "") for m in resp.json().get("models", [])]
                        if not any(model_short in m.lower() for m in running):
                            break
                except Exception:
                    break
            # Ollama 내부 Metal 전환 안정화 대기 (120초)
            # Qwen Metal 해제 후 macOS unified memory 재배치 시간 확보
            # 30초로는 embed 시작 시 OOM 재현됨
            time.sleep(120)
            logger.info(f"메인 모델 언로드 확인: {self.model}")
        except Exception as e:
            logger.debug(f"모델 언로드 요청 실패 (무시): {e}")

    def unload_embed_model(self) -> None:
        """fastembed 참조 해제 — GC로 ONNX 메모리 반환 (Ollama API 불필요)."""
        if hasattr(self, '_fastembed_model') and self._fastembed_model is not None:
            self._fastembed_model = None
            import gc
            gc.collect()
        logger.info("임베딩 모델 언로드 완료 (fastembed 참조 해제)")

    # ── 텍스트 청킹 ──────────────────────────────────────

    def chunk_text(self, text: str, file_type: str = "doc") -> List[str]:
        max_words = self.config.max_words_per_chunk
        words = text.split()
        if len(words) <= max_words:
            return [text]

        segments = _split_by_headings(text, file_type)
        if len(segments) > 1:
            return _merge_segments(segments, max_words)
        return _word_split(text, max_words)

    # ── 메타데이터 추출 ──────────────────────────────────

    def extract_metadata(self, text: str, filename: str) -> Dict:
        defaults: Dict = {
            "doc_date": "unknown",
            "version": "unknown",
            "organization": "unknown",
            "doc_type": "unknown",
            "cves": [],
            "cwes": [],
            "affected_systems": [],
        }

        system = self.config.get_prompt("system")
        prompt_tmpl = self.config.get_prompt("extract_metadata")
        if not prompt_tmpl:
            return defaults

        sample = " ".join(text.split()[:1500])
        prompt = _fmt(prompt_tmpl, filename=filename, content=sample)
        result = self._chat(system, prompt, temperature=0.1, num_ctx=self.config.num_ctx_for("metadata"))

        try:
            yaml_text = result.strip()
            if "```yaml" in yaml_text:
                start = yaml_text.index("```yaml") + 7
                end = yaml_text.index("```", start)
                yaml_text = yaml_text[start:end].strip()
            elif yaml_text.startswith("```"):
                start = yaml_text.index("```") + 3
                end = yaml_text.index("```", start)
                yaml_text = yaml_text[start:end].strip()

            parsed = yaml.safe_load(yaml_text)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if v is not None and v != "":
                        defaults[k] = v
        except Exception as e:
            logger.warning(f"메타데이터 YAML 파싱 실패 [{filename}]: {e}")

        return defaults

    # ── 목차 추출 ─────────────────────────────────────────

    def extract_toc(self, text: str, filename: str) -> str:
        system = self.config.get_prompt("system")
        prompt_tmpl = self.config.get_prompt("extract_toc")
        if not prompt_tmpl:
            return ""

        sample = " ".join(text.split()[:3000])
        prompt = _fmt(prompt_tmpl, filename=filename, content=sample)
        result = self._chat(system, prompt, temperature=0.1, num_ctx=self.config.num_ctx_for("toc"))
        return result.strip()

    # ── 도메인 분류 ──────────────────────────────────────

    def classify_domain(self, text: str, filename: str = "") -> str:
        system = self.config.get_prompt("system")
        classify_tmpl = self.config.get_prompt("classify")

        sample = " ".join(text.split()[:800])
        prompt = _fmt(
            classify_tmpl,
            domains=", ".join(self.config.domains),
            filename=filename,
            content=sample,
        )

        result = self._chat(system, prompt, temperature=0.1, num_ctx=self.config.num_ctx_for("classify"))

        for domain in self.config.domains:
            if domain in result:
                return domain
        return "스크랩"

    def classify_and_extract_metadata(self, text: str, filename: str = "") -> tuple:
        """도메인 분류 + 메타데이터 추출을 단일 LLM 호출로 처리. 반환: (domain, metadata_dict)"""
        defaults: Dict = {
            "doc_date": "unknown", "version": "unknown", "organization": "unknown",
            "doc_type": "unknown", "cves": [], "cwes": [], "affected_systems": [],
        }

        system = self.config.get_prompt("system")
        tmpl = self.config.get_prompt("classify_and_metadata")
        if not tmpl:
            domain = self.classify_domain(text, filename)
            metadata = self.extract_metadata(text, filename)
            return domain, metadata

        sample = " ".join(text.split()[:1500])
        prompt = _fmt(
            tmpl,
            domains=", ".join(self.config.domains),
            filename=filename,
            content=sample,
        )
        result = self._chat(system, prompt, temperature=0.1, num_ctx=self.config.num_ctx_for("classify_metadata"))
        logger.info(f"classify_and_metadata LLM 원시 응답 [{filename}]:\n{result[:500]}")

        # 도메인 파싱: "도메인: AWS" 형식의 첫 줄
        domain = "스크랩"
        for line in result.splitlines():
            stripped = line.strip()
            if stripped.startswith("도메인:"):
                candidate = stripped.split(":", 1)[1].strip()
                for d in self.config.domains:
                    if d in candidate:
                        domain = d
                        break
                break
        # 첫 줄 파싱 실패 또는 unknown 응답 시 전체 텍스트에서 도메인명 탐색 (fallback)
        if domain == "스크랩":
            for d in self.config.domains:
                if d != "스크랩" and d in result:
                    domain = d
                    break
        # 파일명 기반 최종 fallback (LLM이 unknown 반환 시)
        if domain == "스크랩" and filename:
            filename_domain_map = [
                (["주요정보통신기반시설", "취약점분석", "취약점평가", "ISMS", "ISMS-P", "보안점검", "취약점진단"], "취약점진단"),
                (["소스코드", "SAST", "시큐어코딩"], "소스코드진단"),
                (["AWS", "Azure", "GCP", "클라우드"], "AWS"),
                (["모바일", "Android", "iOS", "APK"], "모바일진단"),
                (["리버싱", "reversing", "바이너리", "악성코드"], "리버싱"),
                (["암호", "PKI", "TLS", "SSL", "AES", "RSA"], "암호학"),
                (["모의해킹", "pentest", "CTF", "버프스위트"], "모의해킹"),
                (["EQST", "KARA", "위협인텔", "침해사고", "랜섬웨어"], "관제"),
            ]
            for keywords, mapped_domain in filename_domain_map:
                if any(kw in filename for kw in keywords):
                    domain = mapped_domain
                    logger.info(f"파일명 기반 도메인 fallback [{filename}]: {domain}")
                    break

        # YAML 파싱
        try:
            yaml_text = result
            if "```yaml" in yaml_text:
                start = yaml_text.index("```yaml") + 7
                end = yaml_text.index("```", start)
                yaml_text = yaml_text[start:end].strip()
            elif "```" in yaml_text:
                start = yaml_text.index("```") + 3
                end = yaml_text.index("```", start)
                yaml_text = yaml_text[start:end].strip()
            else:
                yaml_text = ""

            if yaml_text:
                parsed = yaml.safe_load(yaml_text)
                if isinstance(parsed, dict):
                    for k, v in parsed.items():
                        if v is not None and v != "":
                            defaults[k] = v
        except Exception as e:
            logger.warning(f"classify_and_metadata YAML 파싱 실패 [{filename}]: {e}")

        return domain, defaults

    def classify_ideas_topic(self, text: str, filename: str = "", existing_topics: Optional[List[str]] = None) -> str:
        """02_Ideas 전용: LLM이 구체적 토픽명 자유 생성 (기존 폴더명 우선 재사용)"""
        system = self.config.get_prompt("system")
        tmpl = self.config.get_prompt("classify_ideas_topic")
        if not tmpl:
            return "스크랩"

        sample = " ".join(text.split()[:800])
        topics_str = ", ".join(existing_topics) if existing_topics else "(없음)"
        prompt = _fmt(tmpl, filename=filename, content=sample, existing_topics=topics_str)
        result = self._chat(system, prompt, temperature=0.2, num_ctx=self.config.num_ctx_for("classify"))

        # 첫 줄만, 공백/특수문자 정리
        topic = result.strip().splitlines()[0].strip()
        topic = re.sub(r'[^\w가-힣\-]', '', topic)[:20]
        return topic if topic else "스크랩"

    def evaluate_folder_merge(
        self, name_a: str, sample_a: str, name_b: str, sample_b: str
    ) -> dict:
        """두 Ideas 폴더 병합 여부 LLM 판단. 보수적: 의심스러우면 False"""
        system = self.config.get_prompt("system")
        tmpl = self.config.get_prompt("evaluate_folder_merge")
        if not tmpl:
            return {"should_merge": False, "merged_name": "", "reason": "프롬프트 없음"}

        prompt = _fmt(tmpl, name_a=name_a, sample_a=sample_a, name_b=name_b, sample_b=sample_b)
        result = self._chat(system, prompt, temperature=0.1, num_ctx=self.config.num_ctx_for("merge_eval"))

        try:
            json_text = result.strip()
            # JSON 블록 추출
            if "```" in json_text:
                start = json_text.index("{")
                end = json_text.rindex("}") + 1
                json_text = json_text[start:end]
            import json
            parsed = json.loads(json_text)
            if isinstance(parsed, dict) and "should_merge" in parsed:
                return parsed
        except Exception as e:
            logger.warning(f"폴더 병합 판단 파싱 실패: {e} / 원문: {result[:200]}")

        return {"should_merge": False, "merged_name": "", "reason": "파싱 실패"}

    # ── 단일 파일 마크다운 생성 ──────────────────────────

    def summarize_document(
        self, text: str, filename: str, file_type: str, toc: str = ""
    ) -> str:
        system = self.config.get_prompt("system")

        if file_type == "code":
            lang = get_code_language_from_name(filename)
            prompt_tmpl = self.config.get_prompt("summarize_code")
            prompt = _fmt(
                prompt_tmpl,
                filename=filename,
                language=lang,
                content=text[: self.config.max_words_per_chunk * 6],
            )
            result = self._chat(system, prompt, temperature=0.2, num_ctx=self.config.num_ctx_for("summarize"))
            self.unload_model()
            return result

        chunks = self.chunk_text(text, file_type)

        toc_brief = toc[:1500] if len(toc) > 1500 else toc
        toc_section = f"문서 구조 (목차):\n{toc_brief}\n\n" if toc else ""

        if len(chunks) == 1:
            prompt_tmpl = self.config.get_prompt("summarize_single")
            prompt = _fmt(prompt_tmpl, filename=filename, toc=toc_section, content=chunks[0])
            result = self._chat(system, prompt, temperature=0.3, num_ctx=self.config.num_ctx_for("summarize"))
            self.unload_model()
            return _remove_unknown_sections(result)

        # 다중 청크: 모델 워밍업 후 각 청크 요약
        self.warmup()
        chunk_prompt_tmpl = self.config.get_prompt("summarize_chunk")
        chunk_summaries = []
        for i, chunk in enumerate(chunks, 1):
            chunk_prompt = _fmt(
                chunk_prompt_tmpl,
                filename=filename,
                chunk_num=str(i),
                total_chunks=str(len(chunks)),
                toc=toc_section,
                content=chunk,
            )
            summary = self._chat(system, chunk_prompt, temperature=0.3, num_ctx=self.config.num_ctx_for("summarize"))
            # H1 헤딩 및 상단 아티팩트 H2 헤딩 제거 (문서 내용 정리, 내용 정리, 요약 등)
            summary = re.sub(r"^#\s+[^\n]*\n+", "", summary.lstrip())
            summary = re.sub(r"^##\s+[^\n]*(?:정리|요약|분석|개요)[^\n]*\n+", "", summary)
            chunk_summaries.append(summary)
            logger.info(f"청크 {i}/{len(chunks)} 요약 완료: {filename}")

        # 청크 합본: 상세 내용은 직접 병합 (2차 요약 생략 → 정보 손실 없음)
        combined_detail = "\n\n---\n\n".join(chunk_summaries)

        # 메타 섹션(요약/키워드/관련개념)만 LLM으로 생성 (짧은 입력 → 충분한 출력 공간)
        meta_prompt = _fmt(
            self.config.get_prompt("summarize_meta"),
            filename=filename,
            toc=toc_section,
            content=combined_detail[:6000],
        )
        meta_result = self._chat(system, meta_prompt, temperature=0.3, num_ctx=self.config.num_ctx_for("meta_summary"))

        # Qwen 언로드: nomic ONNX 추론이 Qwen Metal(9.9GB)과 Unified Memory를 동시에
        # 경쟁하면 Docker VM 물리 RAM 부족 → OOM(exitCode=137) 발생 확인됨.
        # suggest_links는 link_note 내에서 Qwen을 재로드해 사용한다.
        self.unload_model()

        return _assemble_multi_chunk_note(meta_result, combined_detail)

    # ── 합본 노트 생성 ───────────────────────────────────

    def create_merged_note(self, notes_content: List[str], source_titles: List[str]) -> str:
        system = self.config.get_prompt("system")
        titles_str = ", ".join(source_titles)
        combined = "\n\n---\n\n".join(notes_content[:6000])

        prompt = f"""다음은 유사도가 높아 합본으로 통합되는 {len(source_titles)}개의 노트입니다.
출처: {titles_str}

각 노트의 핵심 내용을 종합하여 하나의 완성된 마크다운 노트로 작성하세요.
중복 내용은 제거하고, 상호보완적인 내용은 통합하세요.

노트 내용:
{combined}

통합 노트를 작성하세요."""

        return self._chat(system, prompt, temperature=0.3, num_ctx=self.config.num_ctx_default)

    # ── 링크 제안 ────────────────────────────────────────

    def suggest_links(self, note_content: str, existing_notes: List[dict]) -> List[str]:
        if not existing_notes:
            return []

        system = self.config.get_prompt("system")
        prompt_tmpl = self.config.get_prompt("suggest_links")

        note_list = "\n".join(
            f"- [[{n['title']}]] ({n.get('domain', '')})"
            for n in existing_notes[:50]
        )

        prompt = _fmt(
            prompt_tmpl,
            current_content=note_content[:2000],
            existing_notes=note_list,
        )

        result = self._chat(system, prompt, temperature=0.2, num_ctx=self.config.num_ctx_for("links"))

        links = []
        for line in result.split("\n"):
            line = line.strip()
            if "[[" in line and "]]" in line:
                start = line.index("[[") + 2
                end = line.index("]]", start)
                links.append(line[start:end])

        return links

    # ── Brain Health Check ───────────────────────────────

    def health_check(self, vault_summary: str) -> str:
        system = self.config.get_prompt("system")
        prompt_tmpl = self.config.get_prompt("health_check")
        prompt = _fmt(prompt_tmpl, vault_summary=vault_summary)
        return self._chat(system, prompt, temperature=0.4, num_ctx=self.config.num_ctx_for("health"))

    # ── 검증 파이프라인 메서드 ────────────────────────────

    @staticmethod
    def _extract_json(text: str) -> dict:
        """LLM 응답에서 JSON 블록 추출"""
        import json
        text = text.strip()
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            text = match.group(1)
        else:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                text = text[start:end]
        try:
            return json.loads(text)
        except Exception:
            return {}

    def evaluate_note(self, title: str, domain: str, content: str) -> dict:
        """
        노트 품질 평가.
        반환: {verdict: good|insufficient_topics|critical_error, reason: str, missing_topics: list}
        """
        system = (
            "You are a strict cybersecurity knowledge base reviewer. "
            "Evaluate the given technical note and return a JSON verdict. "
            "Respond ONLY with valid JSON."
        )
        prompt = _fmt(
            """Evaluate this cybersecurity note:

Title: {title}
Domain: {domain}

Content:
{content}

Evaluation criteria:
- good: Content is technically accurate, complete, and well-structured
- insufficient_topics: Key concepts mentioned in title/tags are not explained
- critical_error: Contains factually wrong information, contradictions, or misleading content

Return JSON:
{{"verdict": "good|insufficient_topics|critical_error", "reason": "brief explanation", "missing_topics": ["topic1", "topic2"]}}

Rules:
- missing_topics is empty list for good or critical_error
- reason must be in Korean (한국어)
- Do NOT flag missing CVE information as an error""",
            title=title,
            domain=domain,
            content=content[:3000],
        )
        raw = self._chat(system, prompt, temperature=0.1, num_ctx=self.config.num_ctx_for("classify"))
        result = self._extract_json(raw)
        if not isinstance(result, dict) or result.get("verdict") not in {"good", "insufficient_topics", "critical_error"}:
            raise ValueError("노트 평가 응답에 유효한 verdict가 없습니다")
        if not isinstance(result.get("missing_topics", []), list):
            raise ValueError("missing_topics는 배열이어야 합니다")
        return result

    def attempt_correction(self, title: str, domain: str, content: str, reason: str) -> str:
        """
        중대 오류가 있는 노트를 로컬 LLM이 수정 시도.
        반환: 수정된 노트 본문
        """
        system = (
            "You are a cybersecurity expert. "
            "Correct the factual errors in the following technical note. "
            "Preserve structure and write in Korean (한국어). "
            "Do not add CVE references."
        )
        prompt = _fmt(
            """Technical note with critical error:

Title: {title}
Domain: {domain}
Error identified: {reason}

Current content:
{content}

Rewrite the note content fixing all identified errors while preserving the markdown structure:""",
            title=title,
            domain=domain,
            reason=reason,
            content=content[:3000],
        )
        return self._chat(system, prompt, temperature=0.2, num_ctx=self.config.num_ctx_for("summarize"))

    def self_validate(self, title: str, corrected_content: str) -> dict:
        """
        수정 후 자기 검증.
        반환: {still_critical: bool, reason: str}
        """
        system = (
            "You are a cybersecurity fact-checker. "
            "Re-evaluate corrected content strictly. "
            "Respond ONLY with valid JSON."
        )
        prompt = _fmt(
            """Re-evaluate this corrected cybersecurity note:

Title: {title}

Corrected content:
{content}

Is there still a critical factual error?
Return JSON: {{"still_critical": true|false, "reason": "Korean explanation"}}""",
            title=title,
            content=corrected_content[:3000],
        )
        raw = self._chat(system, prompt, temperature=0.1, num_ctx=self.config.num_ctx_for("classify"))
        result = self._extract_json(raw)
        if not isinstance(result, dict) or not isinstance(result.get("still_critical"), bool):
            raise ValueError("자기 검증 응답에 boolean still_critical이 없습니다")
        return result

    def generate_perplexity_queries(self, title: str, missing_topics: list) -> list:
        """
        부족한 주제들에 대한 Perplexity 검색 쿼리 생성 (최대 2개).
        반환: [query_str, ...]
        """
        if not missing_topics:
            return [f"cybersecurity {title} technical overview"]

        system = "You are a cybersecurity research assistant. Generate precise web search queries."
        topics_str = ", ".join(missing_topics[:4])
        prompt = _fmt(
            """Generate {count} specific web search queries for missing cybersecurity topics.

Note title: {title}
Missing topics: {topics}

Return one query per line, no numbering, no explanation:""",
            count=str(min(2, len(missing_topics))),
            title=title,
            topics=topics_str,
        )
        raw = self._chat(system, prompt, temperature=0.2, num_ctx=self.config.num_ctx_for("classify"))
        queries = [line.strip() for line in raw.strip().split("\n") if line.strip()]
        return queries[:2] if queries else [f"cybersecurity {title}"]

    def create_supplement_note(self, title: str, domain: str, missing_topics: list, context: str) -> str:
        """
        부족한 주제 보충 노트 생성 (00_inbox에 새 .md로 저장될 내용).
        반환: 마크다운 본문
        """
        system = (
            "You are a cybersecurity knowledge base author. "
            "Write a focused technical supplement note in Korean (한국어). "
            "Do not add CVE references."
        )
        topics_str = ", ".join(missing_topics)
        prompt = _fmt(
            """Write a supplement note covering missing topics for an existing cybersecurity note.

Original note title: {title}
Domain: {domain}
Missing topics to cover: {topics}

Additional web context:
{context}

Write a concise technical supplement note in Korean covering only the missing topics.
Use markdown with ## headers. Start directly with content (no front matter):""",
            title=title,
            domain=domain,
            topics=topics_str,
            context=context[:2000],
        )
        return self._chat(system, prompt, temperature=0.3, num_ctx=self.config.num_ctx_for("summarize"))

    # ── Gap 분석 파이프라인 메서드 ────────────────────────

    def identify_gaps(self, domain: str, existing_topics: List[str], already_generated: List[str]) -> List[str]:
        """
        도메인의 기존 노트 제목 목록을 보고 빠진 핵심 개념 식별.
        반환: 생성해야 할 주제 문자열 목록 (최대 gaps_per_domain개)
        """
        system = (
            "You are a cybersecurity curriculum expert. "
            "Identify important concepts that are MISSING from an existing knowledge base. "
            "Respond ONLY with one topic per line, no numbering, no explanation."
        )
        topics_str = "\n".join(f"- {t}" for t in existing_topics[:80])
        generated_str = "\n".join(f"- {t}" for t in already_generated[:30]) if already_generated else "없음"
        prompt = _fmt(
            """Knowledge base domain: {domain}

Existing notes (already covered):
{topics}

Already generated gap documents (do NOT suggest these again):
{generated}

Identify up to 5 important cybersecurity concepts for the domain "{domain}" that are MISSING from the existing notes.
Focus on fundamental techniques, attack methods, defensive measures, or tools relevant to {domain}.
Do NOT suggest CVE tracking or CVE-related topics.
Write each missing concept as a short Korean title (20 chars max), one per line:""",
            domain=domain,
            topics=topics_str,
            generated=generated_str,
        )
        raw = self._chat(system, prompt, temperature=0.3, num_ctx=self.config.num_ctx_for("links"))
        lines = [line.strip().lstrip("-•* ") for line in raw.strip().split("\n") if line.strip()]
        return [l for l in lines if l and len(l) <= 60][:5]

    def generate_concept_draft(self, topic: str, domain: str, context: str = "") -> str:
        """
        빠진 개념에 대한 고품질 개념서 초안 생성 (Claude Teacher 역할).
        반환: 마크다운 개념서 본문 (front matter 없음)
        """
        system = (
            "You are a senior cybersecurity engineer writing a technical concept document. "
            "Write in Korean (한국어). Be concise, accurate, and practical. "
            "Do not add CVE references."
        )
        ctx_section = f"\n\nAdditional web context:\n{context[:2000]}" if context else ""
        prompt = _fmt(
            """Write a comprehensive concept document for the following missing topic.

Topic: {topic}
Domain: {domain}{ctx}

Requirements:
- Write entirely in Korean
- Use ## markdown headers for structure
- Include: 개요, 핵심 개념, 실무 적용, 관련 기법/도구
- Length: 400-800 words
- Start directly with content (no front matter, no title line)
- Do NOT add CVE identifiers""",
            topic=topic,
            domain=domain,
            ctx=ctx_section,
        )
        return self._chat(system, prompt, temperature=0.35, num_ctx=self.config.num_ctx_for("summarize"))

    def create_correction_note(self, title: str, domain: str, corrected_content: str, reason: str) -> str:
        """
        Claude API가 수정한 내용을 기반으로 최종 수정 노트 생성.
        반환: 마크다운 본문
        """
        system = (
            "You are a cybersecurity technical writer. "
            "Format the corrected content as a clean markdown note in Korean. "
            "Do not add CVE references."
        )
        prompt = _fmt(
            """Format this corrected cybersecurity note content:

Title: {title}
Domain: {domain}
Original error: {reason}

Corrected content:
{content}

Output a clean, well-structured markdown note starting with the content (no front matter):""",
            title=title,
            domain=domain,
            reason=reason,
            content=corrected_content[:4000],
        )
        return self._chat(system, prompt, temperature=0.2, num_ctx=self.config.num_ctx_for("summarize"))


def get_code_language_from_name(filename: str) -> str:
    from pathlib import Path
    return get_code_language(Path(filename))


def _remove_unknown_sections(text: str) -> str:
    """TTP·IOC 섹션 내용이 'unknown' 등 무의미한 값이면 해당 섹션 제거"""
    _EMPTY_VALUES = {"unknown", "없음", "해당없음", "해당 없음", "n/a", "-", ""}
    _REMOVABLE = ["## ⚔️ 공격 기법 & TTP", "## 🚨 침해지표 (IOC)"]
    for header in _REMOVABLE:
        if header not in text:
            continue
        start = text.index(header)
        rest = text[start + len(header):]
        end = rest.find("\n## ")
        content = rest[:end].strip() if end > 0 else rest.strip()
        if content.lower() in _EMPTY_VALUES:
            section = f"{header}\n{content}" if content else header
            text = text.replace(section, "").strip()
    return text


def _assemble_multi_chunk_note(meta_result: str, combined_detail: str) -> str:
    """메타 LLM 결과(요약/키워드/링크) + 청크 상세 내용으로 최종 노트 조립"""

    def extract_section(text: str, header: str) -> str:
        if header not in text:
            return ""
        start = text.index(header)
        rest = text[start + len(header):]
        end = rest.find("\n## ")
        content = rest[:end].strip() if end > 0 else rest.strip()
        return f"{header}\n{content}"

    summary = extract_section(meta_result, "## 📌 요약")
    ttp = extract_section(meta_result, "## ⚔️ 공격 기법 & TTP")
    ioc = extract_section(meta_result, "## 🚨 침해지표 (IOC)")
    keywords = extract_section(meta_result, "## 💡 핵심 키워드")
    links = extract_section(meta_result, "## 🔗 관련 개념")

    parts = []
    if summary:
        parts.append(summary)
    if ttp:
        parts.append(ttp)
    if ioc:
        parts.append(ioc)
    if keywords:
        parts.append(keywords)
    if links:
        parts.append(links)
    parts.append(f"## 🔍 상세 내용\n\n{combined_detail}")

    return _remove_unknown_sections("\n\n".join(parts))
