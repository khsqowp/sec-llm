# reindex_missing 메타데이터 보존 수정 및 검증

검증일: 2026-10-11 (Asia/Seoul). git add/commit 실행 없음. `.env` 변경 없음. 기존 working tree 변경사항 보존. 실제 vault에는 이번 검증의 파일 생성·수정·삭제 작업을 수행하지 않았다.

## (a) 수정 방식과 코드 검토

- `services/pipeline/src/db.py:165`: `update_qdrant_id(note_path, qdrant_id)` 추가. 기존 행을 대상으로 SQL UPDATE하며 SET 대상은 qdrant_id 하나다. title/domain/tags/source_file, 생성·수정 시각, 검증 상태는 보존한다. 대상 행이 없으면 LookupError를 발생시키며 신규 행을 만들지 않는다.
- `services/pipeline/src/linker.py:581`: 재색인 성공 후 빈 tags/source_file을 전달하던 upsert_note 호출을 전용 UPDATE로 교체했다.
- `tests/test_reindex_metadata.py`: 회귀 테스트 3건 추가.

전용 UPDATE를 선택한 근거: 재색인 입력은 `get_notes_without_qdrant()`에서 가져온 기존 DB 레코드다. 재색인의 책임은 색인 ID 복구이며 문서 메타데이터 재작성은 필요하지 않다. 기존 값을 조회해서 다시 upsert하면 조회와 저장 사이에 갱신된 메타데이터를 오래된 값으로 덮어쓸 수 있다. 단일 컬럼 UPDATE는 그 위험을 제거한다.

전체 services에서 `upsert_note|reindex_missing` 검색 결과, 빈 메타데이터 upsert는 linker의 해당 호출 하나였다. scheduler.py:81/145와 reindex_worker.py:31은 이 함수를 호출할 뿐 별도의 메타데이터 upsert를 하지 않으므로 변경하지 않았다.

processor.py:267은 `write_individual_note()`가 반환한 새 파일 경로에 doc_tags와 입력 파일 경로를 저장한다. processor.py:393은 `write_merged_note()`가 반환한 새 합본 파일 경로에 `["합본"]`과 `merged`를 저장한다. VaultWriter 두 메서드는 `write_new_files()`로 연결되며 `open("x")` 배타 생성과 충돌 시 숫자 suffix를 사용한다. 기존 파일을 덮어쓰는 경로가 아니므로 두 호출은 유지했다.

## (b) 운영 DB 조회와 더미 보존 검증

실제 DB는 `Config.postgres_url` / POSTGRES_URL을 사용하는 PostgreSQL이다. SQLite 파일이 아니다. 비밀번호는 출력하지 않았다. 조회·더미 검증은 `docker exec dolphin-pipeline python ...`에서 수행했다. Database 생성자의 마이그레이션 호출을 피하고 기존 engine/session에 연결했다.

조회한 정상 메타데이터 샘플:

- note_path: `/vault/LocalLLM/01_Security/AWS/2023 클라우드 보안 가이드_AWS Part1.md`
- tags: `["document", "AWS", "클라우드 보안 가이드", "ISMS", "계정 관리", "권한 관리", "가상 리소스 관리", "운영 관리", "IAM", "MFA", "EBS 및 볼륨 암호화 설정"]`
- source_file: `/vault/00_inbox/archive/2023 클라우드 보안 가이드_AWS.pdf`

검증 이후 이 샘플을 다시 조회해 tags/source_file이 그대로임을 확인했다. 이 레코드를 수정하지 않았다.

더미 운영 DB 레코드:

- note_path: `/tmp/dummy_test_note_2aa832782a544299b31ff9dffe768730.md` (파일 생성 없음)
- DB id: 2036
- title/domain: Dummy/security
- tags/source_file: `["테스트"]` / `dummy.txt`
- 갱신 전 qdrant_id: 빈 문자열
- `update_qdrant_id()` 호출 후 qdrant_id: `dummy-qdrant-id`
- 전후 모든 컬럼 비교: qdrant_id를 제외한 모든 컬럼 일치.
- finally에서 ORM 삭제 1건, 재조회 0건. 이후 독립 재조회에서도 0건.

이 더미 검증은 실제 Qdrant 포인트를 생성하지 않았다. 없는 파일 경로는 reindex_missing의 파일 존재 검사에서 건너뛰므로 더미에는 전용 메서드를 직접 호출했다. 전체 reindex_missing 경로는 회귀 테스트의 임시 파일과 격리 SQLite DB에서 실행했다. 임베딩 및 Qdrant 클라이언트는 이 회귀 테스트에서 명시적인 fake를 사용한다.

## (c) 테스트 결과

- 로컬 임시 가상환경에서 `python3 -m unittest discover -s tests`: 31건, OK, 0.240초.
- `node --test tests/brain_polling.test.js`: 6건, pass 6 / fail 0 / skipped 0.
- 운영 컨테이너 Python 3.11 / SQLAlchemy 2.0.30에서 새 테스트만 실행: 3건, OK.
- 운영 컨테이너 임시 소스 복사본에서 전체 unittest: 최종 31건, OK, 0.235초.

회귀 테스트는 (1) qdrant_id 외 모든 컬럼 보존 및 반복 갱신, (2) 없는 행 생성 차단, (3) 실제 reindex_missing 함수의 완료 경로와 두 번째 호출의 대상 0건을 검사한다.

검증 과정의 실패도 기록한다. 로컬 Python 3.14에서 SQLAlchemy 2.0.30은 `__firstlineno__` 호환 오류로 import에 실패했다. 임시 가상환경에 호환 SQLAlchemy 2.1.4를 설치해 로컬 검증했으며 프로젝트 requirements는 수정하지 않았다. 운영 컨테이너에서는 프로젝트 버전 2.0.30으로 통과했다. 컨테이너 테스트 소스 전송 중 macOS AppleDouble `._*.py`가 생겨 명세 테스트가 실패했으나 임시 복사본의 해당 파일만 제거했다. 이후 기존 MOC 테스트가 한 번 `3 != 2`로 실패했고 다음 전체 실행은 통과했다. 기존 MOC 테스트의 간헐적 실패 가능성이 있으며 이번 범위에서는 수정하지 않았다.

## (d) 기능검증

기존 서비스에서 HTTP 검증:

| 요청 | 상태 | 응답 확인 |
|---|---:|---|
| GET /health | 200 | 15 bytes |
| GET /api/models | 200 | 65 bytes |
| POST /api/chat | 200 | general 모드, `OK` 토큰 및 `[DONE]` SSE 이벤트 |
| GET /api/brain/notes | 200 | 32,116 bytes |

채팅에는 session_id를 지정하지 않아 테스트 대화 저장 경로를 사용하지 않았다.

실제 `Storage/inbox/archive_localllm/` 감시기 검증은 수행하지 않았다. 실행 중 감시기가 실제 VAULT_PATH에 노트를 만들기 때문에 “실제 vault 조회만 허용”이라는 명시적 제한과 충돌한다. 대체 검증은 실행 중 dolphin-pipeline 내부의 실제 Processor/LLM/DB/Qdrant를 사용하되 Config 객체의 vault/storage/inbox 경로만 `/tmp/dolphin_reindex_e2e_wm7haa0l/` 아래로 격리했다. `.env`, compose, 운영 설정 파일은 수정하지 않았다. 임시 Config의 LLM timeout=90초/max_retries=1로 검증의 대기를 제한했다. 실제 inbox 감시기 이벤트 전달까지 검증한 것으로 해석하면 안 된다.

격리 검증 및 정리 결과는 아래 최종 실행 결과에 기록한다.

## (e) 재빌드와 기동

`docker compose --env-file .env up -d --build` 실행 결과: 종료 코드 1. pipeline/chat 이미지 빌드는 성공했다. 기동 단계에서 기존 `dolphin-postgres` 컨테이너 이름 충돌로 중단됐다.

기존 PostgreSQL 컨테이너 ID: `c432343382bd35e57526d2e2f3529e4e31dc3075c6042a0c78d3e569e2dcfbde`. Docker Config.Labels는 `{}`여서 현재 Compose의 관리 컨테이너가 아니다. 삭제·이름 변경·데이터 볼륨 변경은 수행하지 않았다. 전체 4개 서비스 Compose 재기동 성공이라고 보고할 수 없다.

충돌 이후 상태: chat/pipeline/qdrant 실행 중, Qdrant healthy. 별도 기존 postgres 실행 중이며 `pg_isready -U dolphin -d dolphin_brain`은 accepting connections. postgres에는 Docker healthcheck가 없다. 최근 10분 네 서비스 로그에서 에러를 발견하지 않았다. Compose의 version obsolete 경고는 기존 설정이며 변경하지 않았다.

## (f) 과거 손상 정황

운영 note_index 조회 결과: 총 1,439건 중 `tags='[]' AND coalesce(source_file,'')=''`인 레코드 749건. 이 조건만으로 749건 모두가 버그 피해라고 확정할 수 없다.

두 샘플에서 DB의 tags/source_file은 비어 있지만 실제 파일 frontmatter에는 비어 있지 않은 tags와 source가 있었다:

1. `/vault/LocalLLM/01_Security/암호학/JtR_FAQ_part3.md`: source `JtR_FAQ_part3.md`, tags에 암호학/document/Incremental Mode/Wordlist/John the Ripper 등이 존재.
2. `/vault/LocalLLM/01_Security/취약점진단/04_CICD_파이프라인_보안진단.md`: source에 원본 파일명, tags에 취약점진단/document/GitHub Actions/pull_request_target 등이 존재.

메타데이터 불일치는 실제로 확인됐다. 그 원인과 전체 손상 규모의 확정은 별도 이력 분석이 필요하다. 과거 데이터 복구는 이번 범위 밖이며 수행하지 않았다.

`/app/src/reindex_debug.log`에서 2026-07-08부터 시작한 reindex_missing 시작 로그 155개를 확인했다(이번 격리 재색인 실행 전 집계). 사용자 배경의 “수백 번”은 현재 파일만으로 그대로 확정하지 않는다.

## 최종 격리 처리 및 정리 결과

실제 Processor 호출은 성공했다. processing_jobs id=488의 status는 completed, error_message는 NULL이었다. 생성 노트는 `/tmp/dolphin_reindex_e2e_wm7haa0l/vault/LocalLLM/01_Security/취약점진단/reindex_metadata_probe.md` 한 개다. DB에는 document 및 추출 키워드 태그, 격리된 입력 파일 source_file이 저장됐다. 입력 파일은 격리 Storage/Security/취약점진단으로 이동했다.

실제 reindex_missing은 이 격리 경로만 대상으로 노트 1개/7청크를 임베딩하고 Qdrant에 저장했으며 반환값은 1이었다. 실제 노트들의 색인을 재실행하지 않았다.

정리 전후:

| 산출물 | 정리 전 | 삭제 | 재조회/재확인 |
|---|---:|---:|---:|
| Qdrant 테스트 노트 포인트 | 7 | 7 | 0 |
| note_index 테스트 노트 | 1 | 1 | 0 |
| processing_jobs 테스트 작업 | 1 | 1 | 0 |
| 격리 파일(이동된 txt, 노트, MOC, 활동 로그) | 4 | 4 | 격리 루트 존재 False |
| 운영 DB 더미 레코드 | 1 | 1 | 0 |

MOC/활동 로그 경로에 Qdrant 포인트는 생성되지 않았고 전후 모두 0이었다. 입력 txt는 이동되어 원래 테스트 inbox에는 남지 않았으며 이동된 파일까지 삭제했다. 실제 vault의 파일은 정리 대상으로 삼지 않았다. 실행 증거인 로그/이 보고서/회귀 테스트는 보존한다.

## 수정 코드 적용 후 최종 상태

기존 PostgreSQL을 보존하면서 `docker compose --env-file .env up -d --no-deps pipeline chat`를 실행했다(종료 코드 0). pipeline은 빌드된 `sha256:682f48830d0754e5727591d0e0b906d1049d4f242a4e241dce7128dad3f1af7f` 이미지로 재생성됐다. 시작 로그에서 DB 연결, 모델 워밍업, 스케줄러 시작, 초기 재링크 0건, `/storage/inbox` 감시 시작 및 대기 상태를 확인했다. chat 이미지는 기존 실행 이미지와 같아 재생성되지 않았다.

최종 서비스 상태:

| 서비스 | 상태 | 확인 근거 |
|---|---|---|
| qdrant | Up, healthy | docker compose ps |
| pipeline | 새 이미지로 Up | docker compose ps, Docker image ID, inbox 감시 시작 로그 |
| chat | Up | docker compose ps, HTTP 요청 |
| postgres | 기존 독립 컨테이너 running | docker inspect, pg_isready accepting connections |

재기동 후 독립 재조회에서도 테스트 note_index=0, processing_jobs=0, Qdrant 포인트=0, 테스트 루트 존재=False였다. GET /health, /api/models, /api/brain/notes는 재기동 후에도 200이었다. git diff --check는 통과했다. 전체 Compose 기동의 PostgreSQL 이름 충돌 자체는 해결하지 않았다.

재기동 후 POST /api/chat도 200이며 `OK` 및 `[DONE]`을 반환했다. 따라서 네 엔드포인트 모두 수정 코드 적용 후 다시 정상 응답했다. 임시 호스트 가상환경은 검증 종료 후 삭제했고 컨테이너의 임시 테스트 소스 복사본은 pipeline 재생성으로 제거됐다. 영구 결과물은 코드 수정 2파일, 회귀 테스트 1파일, 이 보고서 1파일이다.
