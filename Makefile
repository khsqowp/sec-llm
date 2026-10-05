SHELL := /bin/bash
COMPOSE := docker compose -f docker-compose.yml

.PHONY: setup pull-models build up down logs restart status clean

## 최초 1회 셋업 (Ollama 모델 다운로드 + Docker 빌드 + 기동)
setup: pull-models build up

## Ollama 모델 다운로드 (네이티브 Mac에서 실행)
pull-models:
	@echo "📥 LLM 모델 다운로드 중..."
	ollama pull hf.co/cognitivecomputations/dolphin-2.9.4-qwen2.5-14b-gguf:Q5_K_M
	@echo "📥 임베딩 모델 다운로드 중..."
	ollama pull nomic-embed-text
	@echo "✅ 모델 준비 완료"

## Docker 이미지 빌드
build:
	@echo "🔨 Docker 이미지 빌드 중..."
	$(COMPOSE) build --no-cache

## 서비스 기동
up:
	@echo "🚀 서비스 기동 중..."
	$(COMPOSE) up -d
	@echo "✅ 기동 완료"
	@echo ""
	@echo "📬 Inbox: /Volumes/SSD T7/dolphin_ai/Obsidian/00_inbox"
	@echo "📚 Vault:  /Volumes/SSD T7/dolphin_ai/Obsidian"
	@echo "💾 Storage: /Volumes/SSD T7/dolphin_ai/Storage"

## 서비스 중지
down:
	$(COMPOSE) down

## 로그 확인
logs:
	$(COMPOSE) logs -f pipeline

## 파이프라인만 재시작
restart:
	$(COMPOSE) restart pipeline

## 서비스 상태 확인
status:
	$(COMPOSE) ps
	@echo ""
	@echo "Ollama 상태:"
	@curl -s http://localhost:11434/api/tags | python3 -c "import sys,json; d=json.load(sys.stdin); [print(' -', m['name']) for m in d.get('models',[])]" 2>/dev/null || echo "  Ollama 미실행"

## 채팅 UI만 빌드 & 재시작
chat:
	@echo "💬 채팅 UI 빌드 중..."
	$(COMPOSE) build chat
	$(COMPOSE) up -d chat
	@echo "✅ 채팅 UI 실행: http://localhost:7777"

## 볼륨 포함 완전 삭제 (주의: DB 데이터 삭제됨)
clean:
	@echo "⚠️  볼륨까지 삭제합니다. 계속하려면 Enter를 누르세요 (Ctrl+C로 취소)"
	@read confirm
	$(COMPOSE) down -v
	@echo "정리 완료"
