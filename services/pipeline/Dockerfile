FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

# GPG 시계 동기화 문제 우회
RUN echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99no-check \
    && echo 'Acquire::AllowInsecureRepositories "true";' >> /etc/apt/apt.conf.d/99no-check \
    && echo 'APT::Get::AllowUnauthenticated "true";' >> /etc/apt/apt.conf.d/99no-check

# 모든 시스템 패키지 한 번에 설치 (레이어 최소화)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    libreoffice-writer \
    libreoffice-calc \
    libreoffice-impress \
    tesseract-ocr \
    poppler-utils \
    libgl1 \
    libglib2.0-0t64 \
    && rm -rf /var/lib/apt/lists/*

# Tesseract 한국어/영어 tessdata 직접 다운로드
RUN mkdir -p ${TESSDATA_PREFIX} && \
    curl -L -o ${TESSDATA_PREFIX}/kor.traineddata \
    https://github.com/tesseract-ocr/tessdata/raw/main/kor.traineddata && \
    curl -L -o ${TESSDATA_PREFIX}/eng.traineddata \
    https://github.com/tesseract-ocr/tessdata/raw/main/eng.traineddata

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

CMD ["python", "-m", "src.main"]
