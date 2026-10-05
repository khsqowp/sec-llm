"""
reindex_missing() subprocess worker.

ProcessPoolExecutor spawn subprocess에서 ONNX abort()가 발화하면 Python이
parent process에 SIGTERM을 보내 컨테이너를 종료시킨다. 이를 막기 위해
reindex_missing() 전체를 별도 subprocess에서 실행한다.

main process → subprocess.run(reindex_worker) → (내부) ProcessPoolExecutor spawn
abort()가 발화해도 reindex_worker subprocess만 종료 — PID 1은 안전.
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
    count = linker.reindex_missing(db, limit=20, embed_in_subprocess=False)
    print(count, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
