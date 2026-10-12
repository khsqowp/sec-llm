# 개발 명세 대조 감사 결과

감사일: 2026-10-11. 기준: `docs/dev-spec.md` 초안과 작업 시작 시 working tree의 실제 소스.
기존 수정/미추적 파일을 보존했다. git commit/add/history rewrite, 로컬 `.env` 변경, 실제 vault/외부 ARTEX 실행은 하지 않았다.

## 1. 섹션별 발견사항 전체

| 명세 섹션 | 발견사항 | 처리 및 버그 판단 근거 |
|-----------|----------|------------------------|
| 1/3.1 | hermes3 기본 모델 및 BAAI 임베딩 설명이 실제 Qwen GGUF/nomic와 다름 | 정상 설정 차이: 명세 수정 |
| 2/4 | payloads 컬렉션 표기가 실제 `ctf_payloads`와 다름; `security_reports` 누락 | 명세 수정; payload 내용 유지 |
| 3.1 | `/chat` 오탈자, 상세 경로 `{id}` 대신 `{note_id}`, 세션/search/payload/gap/models/health 라우트 누락 | `/api/chat` 코드는 유지; GET/POST/PATCH/DELETE 20개 전체 명세 반영 |
| 3.1 | 검색 설명이 전문 검색이나 실제 SQL은 ILIKE | 명세 수정; GIN 인덱스 존재와 실제 사용 쿼리 구분 |
| 3.1/10 | export가 읽기 전용 `/vault/00_inbox`에 쓰고 watcher는 `/storage/inbox` 감시 | 코드 버그: 쓰기 실패·입력 불일치. Storage inbox로 새 파일 배타적 생성 |
| 3.1/5 | 업로드 충돌 회피가 초 단위 timestamp + wb라 동시/같은 초 충돌 시 덮어씀; filename=None 예외 | 코드 버그: xb + UUID 재시도, 빈 filename 안전 처리 |
| 4 | BM42 설명과 달리 BM25; notes/payloads hybrid와 KB dense-only 혼재 | 정상 설계 차이: 함수별 명세 반영 |
| 4 | rag에 KB 전체 누락; ctf KB main/aux, remediator/diagnostics KB 전체 검색 등 불일치 | 8모드 표를 정확한 `_KB_MAIN/_KB_AUX`와 일치시킴 |
| 4 | FlashRank 반환 dictionary에 `r.id`를 사용해 AttributeError 후 검색 순서 fallback | 코드 버그: `r['id']`. 8개 이하의 후보도 재정렬; 예외 fallback 4→8 일관화 |
| 4 | 상위 8개 이후 dedup; score는 reranker 점수가 아닌 기존 검색 score; 실패/원문 검색 동작 누락 | 명세 보강. 검색 순서/중복 제거 정책은 유지 |
| 4 | remediator/diagnostics 도구 매핑 미정의 → general 도구 fallback | 정상 구현 현황으로 명세 기록; 새 모드명/도구 정책을 임의 결정하지 않음 |
| 5 | 노트/업로드/연결도 3탭과 3000ms 코드는 존재하나 빈 목록·HTTP 오류·네트워크 실패 뒤 polling이 멈춤 | 코드 버그: 열린 업로드 탭에서 finally 기반 반복, 탭 전환 시 해제 |
| 5/6 | failed 작업에 UI `.error` 스타일 사용 | 실제 DB 상태에 맞게 `.failed`로 수정 |
| 5/6 | DB `/vault/...` 절대 경로 앞에 `/vault`를 다시 붙여 상세/graph 파일 읽기 실패 | 코드 버그: 절대/상대 경로 통합 해석; 루트 밖/외부 symlink 읽기 거부 |
| 4/5 | 일부 KB 색인 payload에는 filename이 없어 출처 제목이 빈 문자열 | 구조적 코드 버그: filename이 없으면 source basename을 제목으로 사용; 원문/카테고리는 유지 |
| 5 | DB title과 실제 충돌/Part 파일명이 다르면 wikilink 대상 매칭 실패 | 신규 노트 DB/Qdrant 제목은 실제 stem, MOC도 실제 stem 사용 |
| 5 | marked HTML 및 노트/작업 metadata를 innerHTML로 직접 삽입 | 실제 렌더링 경로의 스크립트/HTML 실행 위험: DOMPurify/HTML escape 적용. 노트/payload 원문 수정 없음 |
| 5 | 그래프 재렌더 시 기존 simulation 미종료; 노드 클릭이 목록 DOM 존재 여부에 의존 | 기존 simulation stop; 목록 재로딩과 무관하게 노드 ID로 상세 열기 |
| 5 | 8도메인 색상은 초안과 일치. index.html은 같은 domain map이 아니라 5모드 chip 색상 | 값 유지, 적용 범위 명세화. 한국어 pipeline 도메인은 미등록색 #888 |
| 5 | UI 목록 최대 300/무필터 graph 최대 500, 추가 목록 pagination UI 없음 | 현재 구현 한계 명세화; pagination 정책 임의 확대 없음 |
| 6 | tags/created_notes가 TEXT[]가 아니라 TEXT(JSON), VARCHAR 길이 누락, file_hash SHA256가 아니라 MD5 | 실제 ORM/DDL에 맞게 명세 수정 |
| 6 | note_index updated_at/validated_at/validation_verdict/reprocess_count 누락; processing_jobs source_type/storage_path/error_message/completed_at 누락 | 두 테이블 모든 컬럼/타입/제약/ORM default 명세화 |
| 6 | pending/processing/done/error 대신 completed/failed/skipped 사용; skipped에는 종결시각 없음 | 명세 상태 수정; 실제 종결상태 skipped도 completed_at 기록하도록 코드 수정 |
| 6 | gap_log/validation_log 및 chat_sessions/chat_messages/rag_gaps 누락 | pipeline 4테이블 전체 컬럼과 chat 3테이블 추가 |
| 7 | `.env.example` 변수 6개는 초안 표와 이미 일치; Compose 26개 environment 줄에 비밀/모델명 리터럴 없음 | 직접 전수 대조, 서비스별 모든 environment 줄 명세화 |
| 7 | app CHAT_MODEL fallback 리터럴 및 pipeline settings 프로퍼티/클라이언트 기본 모델 존재 | 설정 기본값으로 기록. 실제 키/비밀번호 fallback 없음; 정상 기본값 임의 삭제 없음 |
| 7 | .env.example의 키 주석은 gap/validation 전체 비활성화라고 하지만 실제로는 외부 단계만 건너뜀 | `.env.example` 변경 없이 명세에 실제 동작 설명 |
| 7 | `llm.embedding_model` 설정과 실제 FastEmbed backend가 다름 | 기존 정상 backend 유지, 설정/현재 구현 차이 명세화 |
| 3.2/8 | processor만 나열되어 watcher/DB/외부 API/worker/수동 indexer 등 대다수 모듈 누락 | 전체 src Python 27개 역할, 실행 순서, 색인 시점, 일정, 외부 API 명세화 |
| 3.2/6 | 파싱 결과 없음에도 개별 작업이 processing으로 남음; 폴더 자식 실패가 완료 처리로 이어짐 | 코드 버그: 개별 failed 전환, 실패한 자식은 성공 경로로 전달하지 않고 부모 실패로 전파 |
| 3.2 | 중복 처리 Storage 이동 전에 skipped 저장, storage_path는 빈 값 | 실제 이동 후 skipped와 최종 storage_path 기록 |
| 3.2/4 | cosine 기준 0.85/0.90을 RRF 점수에 비교해 합본/중복 탐지 불발 가능 | 코드 버그: 높은 cosine 임계값 검색/중복 검사에는 dense cosine 사용; 낮은 RRF 검색은 유지 |
| 3.2 | notes 구스키마 감지 시 자동 delete_collection으로 기존 색인 삭제 | 코드 버그: 실패를 명시하고 수동 마이그레이션 요구, 기존 컬렉션 보존. 자동 데이터 삭제 제거 |
| 3.2 | LLM 최종 실패 문자열/빈 내용이 산출물로 저장될 수 있음 | 코드 버그: 빈 내용은 재시도, 소진 시 RuntimeError로 처리 실패 전파 |
| 3.2 | 평가 JSON 파싱 실패를 good, 자기 검증 파싱 실패를 수정 완료로 간주 | 코드 버그: 유효 verdict/boolean 타입 확인, 실패는 ValueError. unknown verdict도 good 처리하지 않음 |
| 3.2 | 보충/수정 파일 저장 실패 또는 빈 내용에도 fixed/corrected 성공 기록 가능 | 코드 버그: 새 산출물 경로가 없으면 실패, 완료 표기하지 않음 |
| 10 | vault.py write_text가 동명 기존 노트/합본을 덮어씀 | 코드 버그/명시 제약 위반: 배타적 그룹 생성과 번호 suffix, 실제 Part 링크 경로 유지 |
| 10 | linker의 링크/백링크/sanitize, MOC, 활동 로그가 기존 .md를 수정/append | 명시 제약 위반: 신규 링크는 생성 전 선택; 기존 편집 기능 차단; MOC snapshot/로그 event 새 파일 생성 |
| 10 | Ideas 재편성이 기존 .md rename; mover가 vault 원본도 이동 가능 | 명시 제약 위반: Ideas 이동/자동 재편성 차단; mover vault 원본/대상/참조 폴더 거부 |
| 10 | validator/gap 파일명이 초 단위이며 write_text로 같은 초에 덮어씀 | 코드 버그: 같은 create-only writer 사용 |
| 10 | Config 기본 inbox는 vault/00_inbox이나 Compose는 storage/inbox | 구성 기본값 버그: `/storage/inbox`로 통일. 도메인/MOC/활동 경로의 루트 이탈도 거부 |
| 10 | pipeline vault :ro가 ActivityLogger/MOC 초기화와 새 노트 생성까지 막음 | 코드 버그: pipeline mount만 쓰기 허용, 모든 기존-file mutation 코드 차단. chat vault는 :ro 유지. VAULT_PATH 값 변경 없음 |
| 10 | ARTEX 관련 코드가 실제로 존재 | tools/artex_session_manager.py의 `/api/tasks`, `/api/exploration/findings` GET 및 별도 notes 파일 기록. app은 X-Orchestration 헤더 사용. 해당 도구/다른 repo는 수정하지 않음 |

## 2. 이번 감사에서 실제 수정한 파일

- `docs/dev-spec.md`: 225→436줄. 실제 라우트·모델·RAG·UI·DB·환경변수·모듈 흐름·제약 갱신.
- `docs/audit-report.md`: 이 전체 발견사항/수정 근거/검증 한계 기록.
- `docker-compose.yml`: pipeline의 vault 마운트만 새 파일 생성 가능하게 변경. 실제 경로/env 값은 유지.
- `services/chat/app.py`: FlashRank 반환 해석/작은 후보 재정렬/fallback, export·upload 배타적 생성 및 Storage inbox, Brain 경로 해석/루트 보호, KB 제목의 source basename fallback.
- `services/chat/static/brain.html`: 지속 polling, failed 스타일, metadata escape/Markdown 정제, graph simulation 종료/노드 상세 열기.
- `services/pipeline/src/new_files.py`: 신규 공통 create-only 생성 helper, 그룹 충돌 및 실패 정리.
- `services/pipeline/src/vault.py`: helper 기반 새 노트/합본 생성; 분할 이동 링크와 빈 파일명 처리.
- `services/pipeline/src/moc.py`: 새 MOC snapshot, 실제 링크 stem, 경로 검증/동일 writer 내 잠금.
- `services/pipeline/src/activity.py`: 기존 활동 파일 append 대신 새 event 파일 생성.
- `services/pipeline/src/linker.py`: 생성 전 링크 선택, 기존 편집/재링크/정제 차단, cosine/RRF 기준 분리, 구스키마 자동 컬렉션 삭제 제거.
- `services/pipeline/src/processor.py`: 링크를 생성 본문에 포함, 실제 stem DB/합본 Qdrant 기록, MOC stem, 파싱/폴더 실패·중복 이력 상태 수정.
- `services/pipeline/src/config.py`: Storage inbox 기본값, 도메인/MOC/활동 파일 경로 루트 보호.
- `services/pipeline/src/mover.py`: vault 원본/대상/참조를 이동하지 않는 guard.
- `services/pipeline/src/ideas_organizer.py`: 기존 vault 자동 재편성/파일 이동 차단, 후보 분석 코드는 유지.
- `services/pipeline/src/relink_isolated.py`: 잘못된 legacy imports(`database` 모듈 없음, 상대 import 호환 불가)를 실행하는 경로 대신 명시적 기존 노트 재링크 차단. 고립 노트 판별 helper 유지.
- `services/pipeline/src/relink_worker.py`: 기존 수정 기능을 실행하지 않고 경고/0건 반환; 불필요한 DB/Qdrant/모델 초기화 제거, worker 반환 계약 유지.
- `services/pipeline/src/db.py`: skipped도 종결시각 기록.
- `services/pipeline/src/validator.py`: 배타적 새 파일 생성, 실패/빈 산출물의 성공 표기 방지.
- `services/pipeline/src/gap_analyzer.py`: 동명 개념서도 새 파일로 생성.
- `services/pipeline/src/llm.py`: 빈 응답/재시도 소진 오류 전파, 평가/자기 검증 응답 구조 검증. 프롬프트 문자열은 유지.
- `tests/test_audit.py`: Python 격리 회귀 검증 28개.
- `tests/brain_polling.test.js`: 실제 UI 함수 기반 Node 회귀 검증 6개.

기존 사용자 변경인 `config/settings.yaml`, `services/chat/static/index.html`, pipeline requirements/parser, 미추적 `index_owasp.py`/ARTEX 도구 등은 감사 시작 전부터 존재했다. 이번 작업은 이를 되돌리거나 커밋하지 않았다. 따라서 최종 `git diff --stat`는 위 감사 변경 외에 기존 변경도 포함하며, 미추적 파일은 표시하지 않는다.

## 3. 수정하지 않은 사용자 검토 항목

- CTF system prompt, `_seed_payloads`의 seed 내용, bypass system prompt는 시작 시점과 AST 대조하여 변경 없음을 확인했다. 내용의 적절성/타당성은 판단하지 않았다.
- `owasp_top10`과 `owasp`는 다른 category다. 특정 mode의 정확한 category 필터는 Top10 데이터까지 포함하지 않는다. 전체 KB 모드에는 검색 가능하지만, 필터 통합 여부는 사용자 판단으로 남겼다.
- remediator/diagnostics는 자동 RAG는 있지만 명시적 tool mapping은 없어 general 도구 fallback이다. 정책 변경 없이 현황만 기록했다.
- `tools/artex_session_manager.py`는 ARTEX HTTP 조회와 사용자가 지정한 notes 경로 기록을 한다. Ochestration repo 소스를 자동 로드/수정하는 참조는 발견하지 않았다. pipeline 전체에는 해당 문자열 참조가 없다. 해당 도구는 이번 감사에서 실행/수정하지 않았다.
- 로컬 `.env` VAULT_PATH 빈 값으로 Compose config가 실패한다는 사전 확인 사실은 설정 이슈로 보고만 한다. `.env`/`.env.example`의 실제 경로 값과 비밀값은 변경하지 않았다.

## 4. 검증 결과와 한계

실행:

```text
python3 -m unittest discover -s tests -v
Ran 28 tests — OK

node --test tests/brain_polling.test.js
6 tests — 6 pass — 0 fail

node --check (brain/index HTML의 inline JS 추출본)
PASS

AST parse (services 전체 Python)
PASS

git diff --check
PASS
```

- 실제 Config/VaultWriter/MOC/Activity/Mover는 임시 디렉터리에서 import해 검증했다. 기존 파일 byte 보존, 20개 동시 생성, symlink 충돌, 그룹 생성 실패 정리, 분할 navigation, inbox 보충/개념서 충돌, vault 이동 거부를 포함한다.
- FastAPI/SQLAlchemy/asyncpg/FlashRank 등 서비스 runtime 의존 패키지가 현재 기본 Python에 없다. 해당 서비스 의존 함수/클래스는 **실제 소스 AST 정의**를 추출하고 명시적 모형 객체와 SDK 응답으로 실행했다. 이 결과는 HTTP integration·실제 DB DDL·실제 모델 추론 검증이 아니다.
- 정확한 라우트 집합, `.env.example` 표 집합, 전체 src 파일/주요 DB 컬럼 명세 포함도 자동 대조했다. ORM 타입표는 실제 Column 선언으로 생성했다.
- FlashRank dictionary 반환 형식은 [프로젝트 공식 Ranker 구현](https://github.com/PrithivirajDamodaran/FlashRank/blob/main/flashrank/Ranker.py)에서 추가 확인했다. 로컬에서 설치된 flashrank 0.2.9의 실모델 추론을 수행하지 않았다.
- type checker/linter(ruff/pyright)는 설치되어 있지 않아 실행하지 않았다. coverage 측정과 Docker build/up, Compose config 실행, 라이브 DB/모델/API·브라우저 통합 실행은 수행하지 않았다. 새 build/run 성공을 주장하지 않는다.
- 기본 venv Python 3.14에서 datetime.utcnow deprecation warning 1건이 나타났지만 오류는 없다. 배포 Docker의 Python 3.11 타입/스키마를 임의 변경하지 않았다.
- create-only 파일 생성은 개별 exclusive open을 사용한다. 파일 그룹이 외부 독자에게 일괄 원자적으로 공개되는 트랜잭션은 아니다. MOC 잠금은 같은 writer 인스턴스 내부이며 여러 독립 프로세스의 동시 MOC 업데이트 직렬화까지 보장하지 않는다.
- 기존 노트 재링크/백링크/정제/자동 이동을 비활성화한 것은 기능 누락을 숨기기 위한 조치가 아니라 섹션 10의 기존 파일 불변 정책을 강제하기 위한 동작 변경이다. 새 노트에 최초 링크 생성, 색인 백필, 검증/보충·gap 신규 생성은 유지한다.
