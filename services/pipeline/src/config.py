import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class Config:
    # 환경변수
    ollama_url: str = "http://host.docker.internal:11434"
    qdrant_url: str = "http://qdrant:6333"
    postgres_url: str = ""
    vault_path: Path = Path("/vault")
    storage_path: Path = Path("/storage")
    inbox_path: Path = Path("/vault/00_inbox")
    config_path: Path = Path("/config")
    perplexity_api_key: str = ""
    anthropic_api_key: str = ""

    # settings.yaml 에서 로드
    settings: dict = field(default_factory=dict)
    prompts: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Config":
        cfg = cls(
            ollama_url=os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434"),
            qdrant_url=os.environ.get("QDRANT_URL", "http://qdrant:6333"),
            postgres_url=os.environ.get("POSTGRES_URL", ""),
            vault_path=Path(os.environ.get("VAULT_PATH", "/vault")),
            storage_path=Path(os.environ.get("STORAGE_PATH", "/storage")),
            inbox_path=Path(os.environ.get("INBOX_PATH", "/vault/00_inbox")),
            config_path=Path(os.environ.get("CONFIG_PATH", "/config")),
            perplexity_api_key=os.environ.get("PERPLEXITY_API_KEY", ""),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        )

        settings_file = cfg.config_path / "settings.yaml"
        prompts_file = cfg.config_path / "prompts.yaml"

        if settings_file.exists():
            with open(settings_file, "r", encoding="utf-8") as f:
                cfg.settings = yaml.safe_load(f)

        if prompts_file.exists():
            with open(prompts_file, "r", encoding="utf-8") as f:
                cfg.prompts = yaml.safe_load(f)

        return cfg

    # ── 편의 프로퍼티 ──────────────────────────────────────

    @property
    def llm_model(self) -> str:
        return self.settings.get("llm", {}).get(
            "model",
            "hf.co/mradermacher/Qwen2.5-Coder-14B-Instruct-Uncensored-GGUF:Q4_K_M",
        )

    @property
    def embedding_model(self) -> str:
        return self.settings.get("llm", {}).get("embedding_model", "nomic-embed-text")

    @property
    def llm_timeout(self) -> int:
        return self.settings.get("llm", {}).get("timeout", 600)

    @property
    def max_retries(self) -> int:
        return self.settings.get("llm", {}).get("max_retries", 3)

    @property
    def max_words_per_chunk(self) -> int:
        return self.settings.get("processing", {}).get("max_words_per_chunk", 2500)

    @property
    def max_words_per_note(self) -> int:
        return self.settings.get("processing", {}).get("max_words_per_note", 4000)

    @property
    def similarity_threshold(self) -> float:
        return self.settings.get("processing", {}).get("similarity_threshold", 0.80)

    @property
    def stability_wait(self) -> float:
        return self.settings.get("processing", {}).get("stability_wait", 2.0)

    @property
    def worker_threads(self) -> int:
        return self.settings.get("processing", {}).get("worker_threads", 2)

    @property
    def skip_extensions(self) -> List[str]:
        return self.settings.get("processing", {}).get(
            "skip_extensions",
            [".class", ".exe", ".dll", ".so", ".dylib", ".bin",
             ".zip", ".tar", ".gz", ".7z", ".DS_Store"],
        )

    @property
    def code_extensions(self) -> List[str]:
        return self.settings.get("processing", {}).get(
            "code_extensions",
            [".py", ".java", ".js", ".ts", ".tsx", ".jsx",
             ".c", ".cpp", ".h", ".go", ".rs", ".sh", ".bash", ".sql"],
        )

    @property
    def domains(self) -> List[str]:
        return self.settings.get(
            "domains",
            ["관제", "네트워크", "리눅스", "AWS", "모의해킹", "취약점진단",
             "소스코드진단", "암호학", "리버싱", "모바일진단", "아이디어", "스크랩"],
        )

    @property
    def ideas_merge_threshold(self) -> float:
        return self.settings.get("ideas", {}).get("merge_similarity_threshold", 0.90)

    @property
    def ideas_min_notes_to_merge(self) -> int:
        return self.settings.get("ideas", {}).get("min_notes_to_merge", 2)

    @property
    def ideas_reorganize_enabled(self) -> bool:
        return self.settings.get("ideas", {}).get("reorganize_on_health_check", True)

    @property
    def domain_folders(self) -> Dict[str, str]:
        return self.settings.get("vault", {}).get("domain_folders", {})

    @property
    def storage_domain_folders(self) -> Dict[str, str]:
        return self.settings.get("vault", {}).get("storage_domain_folders", {})

    @property
    def activity_log_path(self) -> Path:
        log_name = self.settings.get("vault", {}).get("activity_log", "00_activity_log.md")
        return self.vault_path / log_name

    @property
    def num_ctx_default(self) -> int:
        return self.settings.get("llm", {}).get("num_ctx", 16384)

    def num_ctx_for(self, task: str) -> int:
        """태스크명에 맞는 num_ctx 반환. 미설정 시 num_ctx_default."""
        key = f"num_ctx_{task}"
        return self.settings.get("llm", {}).get(key, self.num_ctx_default)

    def get_prompt(self, key: str) -> str:
        return self.prompts.get(key, "")

    def vault_folder_for_domain(self, domain: str) -> Path:
        relative = self.domain_folders.get(domain)
        if relative:
            return self.vault_path / relative
        # 미등록 도메인 = 아이디어 토픽 → 02_Ideas/{topic}/
        return self.vault_path / "02_Ideas" / domain

    def storage_folder_for_domain(self, domain: str) -> Path:
        relative = self.storage_domain_folders.get(domain)
        if relative:
            return self.storage_path / relative
        # 미등록 도메인 = 아이디어 토픽 → Ideas/{topic}/
        return self.storage_path / "Ideas" / domain

    # ── 검증 설정 프로퍼티 ────────────────────────────────

    @property
    def validation_enabled(self) -> bool:
        return self.settings.get("validation", {}).get("enabled", True)

    @property
    def validation_daily_limit(self) -> int:
        return self.settings.get("validation", {}).get("daily_limit", 50)

    @property
    def validation_perplexity_model(self) -> str:
        return self.settings.get("validation", {}).get("perplexity_model", "sonar-pro")

    @property
    def validation_claude_model(self) -> str:
        return self.settings.get("validation", {}).get("claude_model", "claude-sonnet-4-6")

    @property
    def validation_max_perplexity_queries(self) -> int:
        return self.settings.get("validation", {}).get("max_perplexity_queries", 2)

    # ── Gap 분석 설정 프로퍼티 ────────────────────────────

    @property
    def gap_analysis_enabled(self) -> bool:
        return self.settings.get("gap_analysis", {}).get("enabled", True)

    @property
    def gap_analysis_daily_limit(self) -> int:
        return self.settings.get("gap_analysis", {}).get("daily_limit", 5)

    @property
    def gap_analysis_domains_per_run(self) -> int:
        return self.settings.get("gap_analysis", {}).get("domains_per_run", 2)

    @property
    def gap_analysis_gaps_per_domain(self) -> int:
        return self.settings.get("gap_analysis", {}).get("gaps_per_domain", 3)

    @property
    def gap_analysis_perplexity_enabled(self) -> bool:
        return self.settings.get("gap_analysis", {}).get("perplexity_enabled", True)
