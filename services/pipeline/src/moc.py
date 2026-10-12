"""
MOC (Map of Content) 새 스냅샷 생성
도메인별 Obsidian 인덱스 파일 관리
"""
import logging
import re
import threading
from .new_files import write_new_file
from pathlib import Path

logger = logging.getLogger(__name__)


class MocWriter:
    def __init__(self, vault_path: Path, moc_dir: str = "04_MOC"):
        self._lock = threading.Lock()
        self.moc_dir = (vault_path / moc_dir).resolve()
        if not self.moc_dir.is_relative_to(vault_path.resolve()):
            raise ValueError("MOC 경로가 vault 밖을 참조합니다")
        self.moc_dir.mkdir(parents=True, exist_ok=True)

    def update(self, domain: str, title: str) -> None:
        """링크를 추가한 새 MOC 스냅샷 생성 (기존 파일은 보존)."""
        with self._lock:
            self._update(domain, title)

    def _update(self, domain: str, title: str) -> None:
        domain = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", domain).strip(". ") or "general"
        moc_file = self.moc_dir / f"{domain}_MOC.md"
        link = f"- [[{title}]]"

        snapshots = [moc_file] + list(self.moc_dir.glob(f"{domain}_MOC_[0-9]*.md"))
        snapshots = [p for p in snapshots if p.exists()]
        if snapshots:
            moc_file = max(snapshots, key=lambda p: p.stat().st_mtime_ns)
            content = moc_file.read_text(encoding="utf-8")
            if link in content:
                return
            if not content.endswith("\n"):
                content += "\n"
            moc_file = write_new_file(self.moc_dir / f"{domain}_MOC.md", content + link + "\n")
        else:
            header = f"# {domain} MOC\n\n> 자동 생성된 도메인 인덱스\n\n"
            moc_file = write_new_file(moc_file, header + link + "\n")

        logger.info(f"MOC 갱신: {moc_file.name} ← [[{title}]]")
