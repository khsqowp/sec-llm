"""
기존 vault 노트 재링크 worker. create-only 정책상 경고 후 0건 반환.
기존 scheduler/subprocess 호출 계약을 유지하며 Qdrant/모델을 초기화하지 않는다.
"""
import os
import sys

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import logging


def main():
    logging.getLogger(__name__).warning("기존 vault 노트 수정 금지: 재링크 worker 비활성화")
    print(0, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
