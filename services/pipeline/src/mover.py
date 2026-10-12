"""
원본 파일/폴더를 Storage 디렉토리로 도메인별 이동
"""
import logging
import shutil
from pathlib import Path

from .config import Config

logger = logging.getLogger(__name__)


class FileMover:
    def __init__(self, config: Config):
        self.config = config

    def move_to_storage(self, source: Path, domain: str) -> Path:
        """
        파일 또는 폴더를 Storage/도메인 폴더로 이동.
        동명 파일 충돌 시 _1, _2 suffix 추가.
        반환: 이동된 최종 경로
        """
        vault_root = self.config.vault_path.resolve()
        source_root = source.resolve()
        if source_root.is_relative_to(vault_root) or vault_root.is_relative_to(source_root):
            raise PermissionError("기존 vault 파일/폴더 이동 금지")
        dest_folder = self.config.storage_folder_for_domain(domain)
        if dest_folder.resolve().is_relative_to(vault_root):
            raise PermissionError("vault 내부로 원본 파일 이동 금지")
        if source.is_dir() and any(p.resolve().is_relative_to(vault_root) for p in source.rglob("*")):
            raise PermissionError("vault 파일을 참조하는 폴더 이동 금지")
        dest_folder.mkdir(parents=True, exist_ok=True)

        dest = dest_folder / source.name
        dest = _resolve_conflict(dest)

        try:
            shutil.move(str(source), str(dest))
            logger.info(f"이동 완료: {source} → {dest}")
        except Exception as e:
            logger.error(f"파일 이동 실패 [{source}]: {e}")
            raise

        return dest


def _resolve_conflict(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1
