"""
MOC (Map of Content) 자동 생성/갱신
도메인별 Obsidian 인덱스 파일 관리
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class MocWriter:
    def __init__(self, vault_path: Path, moc_dir: str = "04_MOC"):
        self.moc_dir = vault_path / moc_dir
        self.moc_dir.mkdir(parents=True, exist_ok=True)

    def update(self, domain: str, title: str) -> None:
        """도메인 MOC 파일에 [[title]] 링크 추가 (중복 없이)"""
        moc_file = self.moc_dir / f"{domain}_MOC.md"
        link = f"- [[{title}]]"

        if moc_file.exists():
            content = moc_file.read_text(encoding="utf-8")
            if link in content:
                return
            if not content.endswith("\n"):
                content += "\n"
            moc_file.write_text(content + link + "\n", encoding="utf-8")
        else:
            header = f"# {domain} MOC\n\n> 자동 생성된 도메인 인덱스\n\n"
            moc_file.write_text(header + link + "\n", encoding="utf-8")

        logger.info(f"MOC 갱신: {moc_file.name} ← [[{title}]]")
