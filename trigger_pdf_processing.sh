#!/bin/bash
# SK_Rookies 완료 감지 후 stale PDF 폴더 재처리 트리거

LOG="/tmp/pdf_trigger.log"
STALE_FOLDERS=(
  "/vault/00_inbox/archive/개인정보보호"
  "/vault/00_inbox/archive/애플리케이션보안"
  "/vault/00_inbox/archive/취약점진단가이드"
  "/vault/00_inbox/archive/정보보안기사"
  "/vault/00_inbox/archive/애자일개발방법론"
  "/vault/00_inbox/archive/컨벤션"
  "/vault/00_inbox/archive/EQST"
)

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG"
}

check_sk_done() {
  local status
  status=$(docker exec dolphin-postgres psql -U dolphin dolphin_brain -t -A -c \
    "SELECT status FROM processing_jobs WHERE source_path = '/vault/00_inbox/archive/SK_Rookies' ORDER BY created_at DESC LIMIT 1;")
  echo "$status"
}

check_sk_count() {
  docker exec dolphin-postgres psql -U dolphin dolphin_brain -t -A -c \
    "SELECT COUNT(*) FROM note_index WHERE source_file LIKE '%SK_Rookies%';"
}

trigger_processing() {
  log "=== SK_Rookies 완료 감지! PDF 처리 트리거 시작 ==="

  # stale processing 항목 삭제
  log "Stale processing_jobs 항목 삭제 중..."
  for folder in "${STALE_FOLDERS[@]}"; do
    local count
    count=$(docker exec dolphin-postgres psql -U dolphin dolphin_brain -t -A -c \
      "DELETE FROM processing_jobs WHERE source_path = '$folder' AND status = 'processing'; SELECT ROW_COUNT();")
    log "  삭제: $folder ($count 행)"
  done

  log "dolphin-pipeline 컨테이너 재시작..."
  docker restart dolphin-pipeline
  log "재시작 완료. 파이프라인이 PDF 폴더들을 재처리합니다."
  log "=== 트리거 완료 ==="
}

log "모니터링 시작 (SK_Rookies 완료 대기)"
log "현재 note_index SK_Rookies 수: $(check_sk_count)"

while true; do
  status=$(check_sk_done)
  count=$(check_sk_count)
  log "SK_Rookies 상태: $status | note_index: $count/125"

  if [ "$status" = "completed" ] || [ "$count" -ge 124 ]; then
    trigger_processing
    break
  fi

  sleep 600  # 10분마다 체크
done
