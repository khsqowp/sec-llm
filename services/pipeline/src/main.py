"""
파이프라인 진입점 — 시작 시 초기화 및 inbox 감시 루프
"""
import faulthandler
import logging
import signal
import subprocess
import sys
import time
import threading

faulthandler.enable()  # segfault/SIGABRT 시 stderr에 스택 트레이스 출력

from .activity import ActivityLogger
from .config import Config
from .db import Database
from .llm import LLMClient
from .processor import Processor
from .scheduler import HealthCheckScheduler
from .watcher import Watcher

from pathlib import Path


class PipelineBusy:
    """파이프라인 처리 중 여부를 스레드 안전하게 추적 (중첩 호출 대비 카운터 방식)

    on_idle: count가 0으로 떨어진 뒤 idle_delay초 동안 유지되면 호출되는 콜백.
    중간에 acquire가 들어오면 타이머가 취소된다.
    """
    def __init__(self, idle_delay: int = 60):
        self._count = 0
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._idle_delay = idle_delay
        self._idle_timer: threading.Timer = None
        self._on_idle = None

    def set_on_idle(self, callback):
        self._on_idle = callback

    def acquire(self):
        with self._lock:
            self._count += 1
            self._event.set()
            if self._idle_timer:
                self._idle_timer.cancel()
                self._idle_timer = None

    def release(self):
        with self._lock:
            self._count = max(0, self._count - 1)
            if self._count == 0:
                self._event.clear()
                if self._on_idle:
                    self._idle_timer = threading.Timer(self._idle_delay, self._fire_idle)
                    self._idle_timer.start()

    def _fire_idle(self):
        with self._lock:
            self._idle_timer = None
            if self._count > 0:
                return
        if self._on_idle:
            threading.Thread(target=self._on_idle, daemon=True, name="post-process-check").start()

    def is_busy(self) -> bool:
        return self._event.is_set()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


IGNORE_NAMES = {".DS_Store", "README.md", "Thumbs.db", ".gitkeep"}
IGNORE_PREFIXES = (".", "~$")
PIPELINE_SUBFOLDERS = {"ideas", "archive"}


def _process_existing_inbox(config, processor, busy: PipelineBusy):
    inbox = config.inbox_path
    items = []
    for p in inbox.iterdir():
        if p.name in IGNORE_NAMES or any(p.name.startswith(pf) for pf in IGNORE_PREFIXES):
            continue
        if p.is_dir() and p.name.lower() in PIPELINE_SUBFOLDERS:
            # 파이프라인 서브폴더(ideas/, archive/) 자체는 처리하지 않고 내부 파일만 수집
            for child in sorted(p.iterdir()):
                if child.name in IGNORE_NAMES or any(child.name.startswith(pf) for pf in IGNORE_PREFIXES):
                    continue
                items.append(child)
        else:
            items.append(p)

    if not items:
        return
    logger.info(f"시작 시 inbox 항목 {len(items)}개 발견 — 처리 시작")
    busy.acquire()
    try:
        for item in items:
            try:
                processor.process_path(item)
            except Exception as e:
                logger.error(f"시작 처리 실패 [{item.name}]: {e}")
    finally:
        busy.release()


def _warmup_model(llm):
    """모델을 미리 로드하여 첫 번째 처리 지연을 방지"""
    logger.info("모델 워밍업 중 (Cold Start 방지)...")
    try:
        result = llm._chat("당신은 AI입니다.", "안녕", temperature=0.1)
        logger.info("모델 워밍업 완료")
    except Exception as e:
        logger.warning(f"모델 워밍업 실패 (무시하고 계속): {e}")


def main():
    logger.info("=== Dolphin AI 파이프라인 시작 ===")

    config = Config.load()
    db = Database(config.postgres_url)
    llm = LLMClient(config)
    activity = ActivityLogger(config)
    busy = PipelineBusy(idle_delay=60)
    processor = Processor(config, db)
    watcher = Watcher(config, processor, busy)
    scheduler = HealthCheckScheduler(config, db, llm, activity, busy)

    # inbox 처리 완료 후 60초 안정화 뒤 재링크 + 검증 자동 실행
    busy.set_on_idle(lambda: scheduler.run_post_process_check())

    # 모델 워밍업 (cold start 시 모델 로드)
    _warmup_model(llm)

    # 스케줄러 시작 (시작 즉시 Health Check는 실행하지 않음 — inbox 처리 중 충돌 방지)
    scheduler.start()

    # 시작 시 inbox에 이미 있는 항목 처리
    _process_existing_inbox(config, processor, busy)

    # inbox 처리 완료 후 전체 노트 재링크 (백그라운드)
    # busy를 잡아서 idle 타이머가 재링크 중에 발화하지 않도록 보호
    busy.acquire()

    def _run_initial_relink():
        # relink_all_notes()는 ONNX를 911회 호출 → ORT C++ 스레드가 abort() 타이머를 설정.
        # main process에서 실행하면 del로도 타이머 취소 불가 → subprocess 완전 분리.
        try:
            logger.info("초기 전체 재링크 시작 (subprocess 격리)...")
            result = subprocess.run(
                [sys.executable, "-m", "src.relink_worker"],
                timeout=7200,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                updated = int(result.stdout.strip() or "0")
                logger.info(f"초기 전체 재링크 완료: {updated}개 노트 업데이트")
            else:
                logger.error(
                    f"초기 재링크 subprocess 실패 (rc={result.returncode}): {result.stderr[-500:]}"
                )
        except subprocess.TimeoutExpired:
            logger.error("초기 재링크 타임아웃 (7200s)")
        except Exception as e:
            logger.error(f"초기 재링크 실패: {e}")
        finally:
            busy.release()  # count=0 → 60초 후 idle 콜백 발화

    threading.Thread(target=_run_initial_relink, daemon=True, name="initial-relink").start()

    # inbox 감시 시작
    watcher.start()

    # 종료 시그널 핸들러
    def shutdown(sig, frame):
        import traceback
        logger.warning(f"[SHUTDOWN] 신호 수신: sig={sig}, 정리 중...")
        logger.warning(f"[SHUTDOWN] 콜스택:\n{''.join(traceback.format_stack(frame))}")
        sys.stdout.flush()
        watcher.stop()
        scheduler.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    logger.info(f"[SIGNAL] SIGTERM/SIGINT 핸들러 등록 완료 (PID={threading.main_thread().ident})")

    logger.info("대기 중 — inbox에 파일을 넣으면 자동으로 처리됩니다.")
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
