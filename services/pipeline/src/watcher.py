"""
watchdog 기반 inbox 파일 감시
파일/폴더 안정화 후 Processor에 전달
"""
import logging
import threading
import time
from pathlib import Path
from typing import Set

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import Config
from .processor import Processor

logger = logging.getLogger(__name__)

# 감시 제외 패턴
IGNORE_NAMES = {".DS_Store", "README.md", "Thumbs.db", ".gitkeep"}
IGNORE_PREFIXES = (".", "~$")

# 파이프라인 서브폴더 (폴더 자체는 처리 안 하고 안의 파일만 처리)
PIPELINE_SUBFOLDERS = {"ideas", "archive"}


class InboxHandler(FileSystemEventHandler):
    def __init__(self, config: Config, processor: Processor, pipeline_busy=None):
        super().__init__()
        self.config = config
        self.processor = processor
        self.pipeline_busy = pipeline_busy
        self.stability_wait = config.stability_wait
        self._pending: dict = {}     # path → timer
        self._in_progress: Set[str] = set()
        self._lock = threading.Lock()

    def on_created(self, event: FileSystemEvent):
        self._schedule(event.src_path, event.is_directory)

    def on_moved(self, event: FileSystemEvent):
        self._schedule(event.dest_path, event.is_directory)

    def on_modified(self, event: FileSystemEvent):
        # 파일만 modified 처리 (폴더 수정은 내부 파일 변경으로 무수히 발생)
        if not event.is_directory:
            self._schedule(event.src_path, False)

    def _schedule(self, raw_path: str, is_dir: bool):
        path = Path(raw_path)

        if not self._should_process(path):
            return

        with self._lock:
            if str(path) in self._in_progress:
                return

            # 이미 스케줄된 타이머 취소 (debounce)
            if str(path) in self._pending:
                self._pending[str(path)].cancel()

            timer = threading.Timer(
                self.stability_wait,
                self._process,
                args=[path, is_dir],
            )
            self._pending[str(path)] = timer
            timer.start()

    def _process(self, path: Path, is_dir: bool):
        with self._lock:
            self._pending.pop(str(path), None)
            if str(path) in self._in_progress:
                return
            self._in_progress.add(str(path))

        if self.pipeline_busy:
            self.pipeline_busy.acquire()
        try:
            if not path.exists():
                return

            logger.info(f"처리 시작: {'📁' if is_dir else '📄'} {path.name}")
            self.processor.process_path(path)
        except Exception as e:
            logger.error(f"처리 오류 [{path.name}]: {e}")
        finally:
            with self._lock:
                self._in_progress.discard(str(path))
            if self.pipeline_busy:
                self.pipeline_busy.release()

    def _should_process(self, path: Path) -> bool:
        name = path.name

        if name in IGNORE_NAMES:
            return False
        if any(name.startswith(p) for p in IGNORE_PREFIXES):
            return False
        if path.suffix.lower() in self.config.skip_extensions:
            return False

        try:
            rel = path.relative_to(self.config.inbox_path)
            parts = rel.parts

            # 파이프라인 서브폴더(ideas/, archive/) 자체는 처리 대상 아님
            if len(parts) == 1 and parts[0].lower() in PIPELINE_SUBFOLDERS and path.is_dir():
                return False

            if len(parts) > 1 and not path.is_dir():
                # 파이프라인 서브폴더 직속 파일은 개별 처리 허용
                if len(parts) == 2 and parts[0].lower() in PIPELINE_SUBFOLDERS:
                    return True
                return False
        except ValueError:
            return False

        return True


class Watcher:
    def __init__(self, config: Config, processor: Processor, pipeline_busy=None):
        self.config = config
        self.processor = processor
        self.pipeline_busy = pipeline_busy
        self.observer = Observer()

    def start(self):
        inbox = self.config.inbox_path
        inbox.mkdir(parents=True, exist_ok=True)

        handler = InboxHandler(self.config, self.processor, self.pipeline_busy)
        self.observer.schedule(handler, str(inbox), recursive=True)
        self.observer.start()
        logger.info(f"📬 inbox 감시 시작: {inbox}")

    def stop(self):
        self.observer.stop()
        self.observer.join()
        logger.info("inbox 감시 종료")
