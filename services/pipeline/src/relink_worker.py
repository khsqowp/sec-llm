"""
relink_all_notes() subprocess worker.

main process가 ONNX를 로드하면 ORT C++ 백그라운드 스레드가 abort() 타이머를 설정해
~52초 후 컨테이너 전체를 종료한다. 이를 막기 위해 relink를 완전히 분리된
subprocess에서 실행한다 — subprocess 종료 시 ORT 리소스가 완전히 정리됨.
"""
import os
import sys

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from src.config import Config
from src.db import Database
from src.llm import LLMClient
from src.linker import Linker


def main():
    config = Config.load()
    db = Database(config.postgres_url)
    llm = LLMClient(config)
    linker = Linker(config, llm)
    updated = linker.relink_all_notes(db, qdrant_only=True)
    print(updated, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
