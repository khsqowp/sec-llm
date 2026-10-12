"""
작업 내역을 새 활동 이벤트 파일로 기록 (기존 로그 append 금지)
"""
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import List

from .config import Config
from .new_files import write_new_file

logger = logging.getLogger(__name__)


class ActivityLogger:
    def __init__(self, config: Config):
        self.log_path = config.activity_log_path
        self._ensure_file()

    def _ensure_file(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        source_name: str,
        domain: str,
        created_notes: List[str],
        storage_path: str,
        status: str = "완료",
        error: str = "",
    ):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        notes_list = "\n".join(f"  - [[{Path(n).stem}]]" for n in created_notes)
        if not notes_list:
            notes_list = "  - (없음)"

        entry = f"""
## {now} | {source_name}

- **상태**: {status}
- **도메인**: {domain}
- **생성된 노트**:
{notes_list}
- **원본 저장 위치**: `{storage_path}`
"""
        if error:
            entry += f"- **오류**: {error}\n"

        entry += "\n---\n"

        self._write_entry(entry)

    def log_health_check(self, report: str):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"""
## {now} | 🧠 Brain Health Check

{report}

---
"""
        self._write_entry(entry)

    def _write_entry(self, entry: str, retries: int = 5, delay: float = 0.3):
        for attempt in range(retries):
            try:
                write_new_file(self.log_path, "# 활동 로그\n\n" + entry)
                return
            except OSError as e:
                if e.errno == 35 and attempt < retries - 1:
                    time.sleep(delay * (attempt + 1))
                    continue
                raise
