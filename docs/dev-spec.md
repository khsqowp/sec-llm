# dolphin_ai 개발 명세서

작성일: 2026-10-11

---

## 1. 프로젝트 개요

로컬 LLM 기반 보안 특화 AI 어시스턴트 + 개인 2nd Brain 파이프라인.
Ollama로 구동되는 로컬 LLM(기본 모델은 `.env.example`/`config/settings.yaml`의 Qwen2.5-Coder-14B GGUF)을 사용하며, Qdrant 벡터 DB와 PostgreSQL로 지식 베이스를 관리한다.

**범위 경계:** "주간정리/뉴스수집" 노트 생성은 이 저장소의 범위가 아니다. 그 작업은 별도 git 저장소(`Ochestration`)의 `scripts/sync-md-to-obsidian.sh`가 수행하며 결과를 Obsidian vault 최상위 `Ochestration/` 폴더에 rsync로 적재한다. `dolphin_ai` 소스 전체(`services/`, `tools/`)에는 `Ochestration`/`sync-md-to-obsidian` 문자열 참조가 없다(섹션 10 확인 내용과 동일). 이 문서가 다루는 "노트 생성 파이프라인"은 `dolphin_ai`가 직접 구동하는 `services/pipeline`(아카이브/아이디어 inbox 처리) **하나뿐**이며, 아래 2.1절에서 이 파이프라인과 "2nd Brain"(`/brain` UI, 업로드, 대화 내보내기)의 실제 관계를 다룬다.

---

## 2. 아키텍처

```
[사용자 브라우저]
      │
      ▼
[dolphin-chat :7777]  ← FastAPI (app.py)
      │
      ├── Ollama (host:11434) — LLM 추론
      ├── Qdrant (dolphin-qdrant:6333) — 벡터 검색
      │     ├── notes          (개인 노트 임베딩)
      │     ├── security_kb    (보안 지식 베이스)
      │     ├── ctf_payloads   (CTF 페이로드)
      │     └── security_reports (별도 보고서 색인; 자동 RAG 모드에서는 검색하지 않음)
      └── PostgreSQL (dolphin-postgres:5432)
            ├── note_index     (노트 메타데이터)
            └── processing_jobs (파이프라인 작업 이력)

[dolphin-pipeline]  ← watchdog 기반 파이프라인
      │
      ├── inbox 감시 (./Storage/inbox/)
      ├── 문서 파싱 → LLM 분류/청크 처리
      ├── Obsidian Vault .md 파일 생성
      └── PostgreSQL note_index 저장 → 후처리 Qdrant notes 백필/검증

외부 연동: Perplexity(검색/출처 본문), Anthropic Claude(검증 최후 단계),
수동 KB 색인기의 HTTP 수집. 컨테이너 기본 inbox는 /storage/inbox이다.
```

### 2.1 "노트 생성 파이프라인"과 "2nd Brain"의 실제 관계 — 현재 뒤섞여 있음

원래 기획 의도는 주간정리/아카이브/뉴스수집용 노트 생성 파이프라인과, 대화형 업로드·채팅·RAG용 2nd Brain이 **서로 다른 디렉터리에서 별도로 운영**되는 것이었다. 실제 코드/vault를 추적한 결과는 다음과 같다.

**(a) 실제로 분리돼 있는 부분 — vault 최상위 디렉터리 레벨**

- `Ochestration/`(외부 repo가 rsync로 적재, 주간정리/뉴스수집) vs `LocalLLM/` + `00_inbox/`(`dolphin_ai` pipeline이 쓰는 영역)은 vault 최상위에서 이미 분리돼 있다. 코드 레벨에서도 서로 참조하지 않는다(섹션 1 범위 경계, 섹션 10 참조).
- `services/pipeline` 내부에서도 `inbox/ideas/`(아이디어·치트시트)와 `inbox/archive_localllm/`(보안 문서)는 서로 다른 분류 로직(`processor.py`의 `ideas` vs `security` 파이프라인 분기)과 서로 다른 vault 하위 폴더(`LocalLLM/02_Ideas/*` vs `LocalLLM/01_Security/*`)로 나뉜다(`config/settings.yaml`의 `vault.domain_folders`).

**(b) 실제로 뒤섞여 있는 부분 — "2nd Brain" 입력과 "아카이브" 입력이 inbox 단계부터 구분되지 않음**

- `/api/brain/upload`(채팅 UI "업로드" 탭)와 `/api/sessions/{id}/export`(대화 내보내기)는 둘 다 `${STORAGE_PATH}/inbox` **루트**에 바로 파일을 쓴다(`app.py`의 `inbox_path = os.path.join(STORAGE_PATH, "inbox")`, `inbox = Path(STORAGE_PATH) / "inbox"`). `ideas/`나 `archive_localllm/` 하위 폴더에 넣지 않는다.
- `services/pipeline/src/processor.py`의 분기 주석이 명시하듯 "`inbox/ideas/` → `'ideas'`, `inbox/archive_localllm/` → `'security'`, **그 외 → `'security'`**"이다. 즉 inbox 루트에 떨어지는 파일(업로드/대화 내보내기)은 사람이 `Storage/inbox/archive_localllm/`에 직접 넣는 보안 문서와 **동일한 "security" 파이프라인**으로 처리되어 같은 LLM 도메인 분류(12개 한국어 도메인 + `기타`)를 거쳐 `config.py:vault_folder_for_domain()`에 의해 **같은 `LocalLLM/01_Security/{domain}/` 폴더**에 쓰인다. 코드 상 "2nd Brain 전용" inbox 하위 폴더나 전용 vault 폴더는 존재하지 않는다.
- Qdrant `notes` 컬렉션 payload(`linker.py`)는 `{path, title, domain, content, chunk_idx, total_chunks, note_id}`만 가진다. 출처(사람이 `archive_localllm/`에 직접 넣은 문서 vs 채팅 업로드 vs 대화 내보내기)를 구분하는 필드가 없다. `/api/chat`의 자동 RAG가 `notes`를 검색할 때도 domain 필터를 걸지 않고 전체 컬렉션을 검색한다(섹션 4). `/api/brain/notes`의 `domain` 쿼리 파라미터는 `note_index.domain`으로 필터하지만 이 값은 콘텐츠 분류(관제/네트워크/.../아이디어/스크랩/기타 또는 아이디어 토픽명)일 뿐, "이 노트가 2nd Brain 업로드에서 왔는지 아카이브 투입에서 왔는지"를 구분하는 값이 아니다. 따라서 섹션 3.1의 "노트 목록(도메인 필터)"는 콘텐츠 주제 필터이며 파이프라인-출처 필터가 아니다.
- vault에는 `05_QnA/Security/*`, `05_QnA/Ideas/`라는 폴더가 실제로 존재한다(하위 도메인 폴더 구조는 `LocalLLM/01_Security/*`와 동일하게 미러링됨). 그러나 `dolphin_ai` 소스 전체에 `05_QnA` 문자열 참조는 전혀 없다(grep 결과 0건, 확인일 2026-10-11). 해당 폴더들은 `.md` 파일이 0개이며, 같은 시점 `LocalLLM/`에는 1557개 노트(최신 mtime 당월)가 쌓여 있다. 과거 "2nd Brain을 물리적으로 분리하려던" 시도의 흔적으로 보이나, 현재 pipeline 코드는 이 폴더에 전혀 쓰지 않는 **사실상 빈 폴더**다.
- vault 최상위 `00_inbox/`에도 대화 내보내기 파일 1개(`대화_hi_20260902_124051.md`)가 남아 있다. 이는 과거 export 버그(`docs/audit-report.md` 기록: export가 읽기 전용 `/vault/00_inbox`에 쓰던 버그, 현재는 `/storage/inbox`로 수정됨)가 만든 **잔여 파일**이며, 현재 코드 경로는 더 이상 이 위치에 쓰지 않는다.

**결론:** `dolphin_ai` 범위 안에서 "노트 생성 파이프라인"과 "2nd Brain"은 독립된 두 시스템이 아니라, **동일한 단일 pipeline 서비스**를 공유한다. `/brain` UI(노트/업로드/연결도 탭)는 이 파이프라인이 PostgreSQL(`note_index`)·Qdrant(`notes`)·vault(`LocalLLM/`)에 쌓은 결과를 읽는 조회 계층이며, 별도의 저장 영역을 갖지 않는다. 사람이 수동으로 넣는 아카이브 문서와 채팅으로 업로드/내보내기한 파일은 inbox 단계부터 물리적으로 같은 경로를 거쳐 같은 분류·같은 vault 폴더·같은 Qdrant 컬렉션에 섞여 들어간다.

**분리 방법(제안, 미구현 — 코드/폴더 구조는 사용자 승인 전까지 변경하지 않음):**
1. `/api/brain/upload`와 `/api/sessions/{id}/export`가 쓰는 대상을 inbox 루트가 아니라 별도 하위 폴더(예: `inbox/brain/`)로 바꾸고, `watcher.py`/`main.py`의 `PIPELINE_SUBFOLDERS`에 추가해 `processor.py`가 세 번째 분기(`ideas`/`security`/`brain`)로 처리하게 한다.
2. `config/settings.yaml`의 `vault.domain_folders`/`storage_domain_folders`에 `brain` 파이프라인 전용 루트(예: `LocalLLM/05_QnA/{domain}` 또는 기존 빈 `05_QnA/`를 재사용)를 추가해 `vault_folder_for_domain()`이 분기하도록 한다.
3. Qdrant `notes` payload와 `note_index` 테이블에 출처 필드(예: `origin: "manual_archive" | "brain_upload" | "chat_export"`)를 추가하고, `/api/brain/notes`·`/api/chat`의 자동 RAG 필터에 이 필드를 노출한다.

---

## 3. 서비스 구성

### 3.1 chat 서비스 (`services/chat/`)

| 항목 | 값 |
|------|-----|
| 포트 | 호스트/컨테이너 7777 |
| 프레임워크 | FastAPI |
| 기본 모델 | `$CHAT_MODEL`; 미설정 fallback은 `.env.example`의 Qwen2.5-Coder-14B GGUF 모델명과 일치 |
| 임베딩 | FastEmbed `nomic-ai/nomic-embed-text-v1` (768차원) |
| sparse | FastEmbed `Qdrant/bm25` |
| 리랭킹 | FlashRank `ms-marco-MiniLM-L-6-v2` |

**명시적 라우트 전체** (`app.py`의 GET/POST/PATCH/DELETE):

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/` | 채팅 UI (`index.html`) |
| GET | `/brain` | 2nd Brain UI (`brain.html`) |
| POST | `/api/chat` | 모드별 RAG 및 도구 호출을 포함한 스트리밍 채팅 (SSE) |
| GET | `/api/sessions` | 소유자/관리자 범위의 세션 목록 |
| POST | `/api/sessions` | 세션 생성 (201) |
| GET | `/api/sessions/{session_id}` | 세션 상세와 메시지 이력 |
| PATCH | `/api/sessions/{session_id}` | 제목/모드 변경 |
| DELETE | `/api/sessions/{session_id}` | 세션 삭제 (204) |
| POST | `/api/sessions/{session_id}/export` | 대화를 새 Markdown 파일로 `/storage/inbox`에 내보냄 |
| GET | `/api/search` | 대화 내용 ILIKE 검색; 선택적 세션 범위, 발췌 반환 (전문 검색 인덱스 사용 쿼리는 아님) |
| GET | `/api/payloads` | `ctf_payloads` 검색, 선택적 category 필터 |
| POST | `/api/payloads` | 사용자 제공 payload를 dense/sparse 벡터와 함께 추가 (201) |
| GET | `/api/rag/gaps` | 검색 부족 이력; mode/limit 필터 |
| GET | `/api/models` | Ollama 모델 목록과 기본 모델 |
| GET | `/health` | 프로세스 응답 확인 (`status: ok`); 의존 서비스 상태는 확인하지 않음 |
| POST | `/api/brain/upload` | 파일을 `/storage/inbox`에 배타적으로 새로 저장 |
| GET | `/api/brain/jobs` | 최근 작업과 처리 상태 (limit 1–200) |
| GET | `/api/brain/notes` | 노트 목록/전체 개수/도메인 개수; domain/limit/offset |
| GET | `/api/brain/notes/{note_id}` | DB 메타데이터 + vault 파일 내용; 정수 ID |
| GET | `/api/brain/graph` | 노드와 중복 제거된 무방향 wikilink 엣지; domain 필터 |

`/static`은 StaticFiles 마운트다. FastAPI 자동 `/docs`, `/redoc`, `/openapi.json`, `/docs/oauth2-redirect`는 위 decorator 라우트 표와 별개다.
세션/대화 소유권 판별은 `X-Orchestration-User`, `X-Orchestration-Admin` 헤더를 사용한다. Brain/payload/gap API에는 같은 소유자 필터가 없다.
보안 프롬프트와 payload 문구의 타당성은 이 명세의 감사 범위에서 제외한다.
`/api/sessions/{session_id}/export`와 `/api/brain/upload`는 둘 다 `inbox` 루트에 쓰며, 2.1절에서 설명한 대로 inbox 루트는 "security" 파이프라인(아카이브와 동일한 분류·같은 vault 폴더)으로 처리된다. "2nd Brain" 전용 inbox 하위 폴더는 없다.

### 3.2 pipeline 서비스 (`services/pipeline/`)

| 항목 | 실제 동작 |
|------|-----------|
| 트리거 | watchdog의 생성/이동/수정 이벤트 debounce; 시작 시 기존 inbox 처리 |
| 입력 | 호스트 `./Storage/inbox/` → 컨테이너 `/storage/inbox`; `ideas/`는 아이디어 토픽 분류, `archive_localllm/`와 나머지(2.1절의 브라우저 업로드·대화 내보내기 포함)는 보안 분류 |
| 지원 형식 | PDF/DOCX/PPTX/XLSX, HWP, TXT/MD/TSV/CSV, 코드(SQL 포함), JPG/JPEG/PNG, SQLite3/DB |
| 변환 | Office/PDF는 markitdown 우선, 개별 파서 fallback; HWP는 LibreOffice; 이미지는 Ollama vision → OCR |
| LLM | 도메인·메타데이터·목차 추출, 청크별 요약/통합, 키워드와 링크 제안; `_concept_` 입력은 frontmatter를 읽고 본문 보존 |
| 출력 | 새 vault `.md`, `note_index`, 작업 이력, 도메인별 Storage 원본 보관, 새 MOC 스냅샷·활동 로그 |
| 색인 시점 | 개별 노트는 `qdrant_id=NULL`로 DB 저장 후 별도 백필; 합본은 생성 단계에서 직접 색인 |

전체 흐름:

1. `main.py`가 Config/DB/Processor/Watcher/Scheduler를 초기화하고 시작 시 inbox를 처리한다. `watcher.py`가 안정화 대기 후 Processor에 전달한다.
2. `processor.py`가 `parser.py`로 텍스트를 추출하고 `llm.py`로 분류·요약한다. `config/settings.yaml`에는 한국어 12개 분류 도메인과 `기타` 폴더 매핑이 있다. 이는 UI의 보안 KB 8색 맵과 다른 분류 체계다.
3. `linker.py`는 생성 전에 기존 노트 제목을 실제 파일 stem으로 해석해 링크를 제안한다. `vault.py`/`new_files.py`는 충돌 시 번호 suffix를 붙이고 배타적 생성한다. 분할 노트의 이동 링크는 실제 생성 경로를 가리킨다.
4. `db.py`가 노트/작업을 기록하고 `moc.py`가 새 인덱스 스냅샷, `activity.py`가 새 이벤트 로그를 생성한다. `mover.py`는 inbox 원본을 Storage로 옮기며 vault 원본 이동을 거부한다.
5. `scheduler.py`는 처리 종료 후 60초 안정화 시 Qdrant 누락 백필(최대 100)과 검증을 수행한다. 자정 Health Check는 현황 리포트·백필·검증·gap 분석을 수행한다. `reindex_worker.py`는 별도 실행 가능한 백필 worker이며 현재 scheduler의 백필은 직접 호출이다.
6. `validator.py`는 로컬 평가 → 필요 시 `perplexity.py` 검색/출처 수집 → 최후 단계 `claude_client.py` 검토를 수행하고 보충/수정본을 inbox의 **새 파일**로 만든다. LLM 빈 응답/재시도 소진·평가 JSON 오류·산출물 저장 실패를 성공으로 기록하지 않고 오류로 처리한다. API 키가 없으면 해당 외부 호출만 건너뛴다. 로컬 검증 전체가 꺼지는 것은 아니다.
7. `gap_analyzer.py`는 노트 수가 적은 기술 도메인의 누락 주제를 찾고 선택적 Perplexity 보강 후 새 개념서를 inbox에 생성한다. 자동 실행은 자정 작업에만 포함된다. 별도 메시지 알림 서비스는 없고 활동 파일과 프로세스 로그로 상태를 기록한다.

`relink_worker.py`, `relink_isolated.py`의 기존 노트 재링크, `linker.py`의 백링크/기존 파일 정제, `ideas_organizer.py`의 기존 파일 이동은 신규 파일 생성 전용 정책 때문에 비활성화한다. 신규 노트는 최초 생성 시 링크를 포함하며 기존 노트에는 역방향 링크를 추가하지 않는다.

`index_guidelines.py`, `index_source_collector.py`, `index_owasp.py`는 독립 실행하는 `security_kb` 색인기다. `index_reports.py`는 별도 `security_reports` 컬렉션을 만든다. 이 수동 색인 작업들은 watchdog 자동 흐름에 포함되지 않는다.

---

## 4. 챗봇 모드 (`SYSTEM_PROMPTS`)

코드의 8개 키를 그대로 사용한다. 프롬프트 본문과 payload 내용은 수정하지 않는다.

| 모드 | 역할 구분 | 자동 RAG 소스 (실제 컬렉션/정확한 category 값) |
|------|-----------|------------------------------------------------|
| `general` | 범용 | 자동 RAG 없음; `{context}`가 없어 바로 반환 |
| `rag` | 지식 Q&A | `notes` + `security_kb` 전체 |
| `ctf` | CTF | `notes` + `ctf_payloads` + KB main `vuln_env`, aux `owasp` |
| `pentest` | 취약점 분석 | KB 전체 + aux `vulnerability`, `advisory_go`, `advisory_python` |
| `bypass` | 필터 관련 | KB main `owasp`, aux `vulnerability` |
| `threat` | 위협 정보 | KB main `threat_intel`, aux `compliance` |
| `remediator` | 조치방안 | `notes` + KB 전체 + aux `vulnerability`, `owasp`, `advisory_go`, `advisory_python` |
| `diagnostics` | 시스템 진단 | `notes` + KB 전체 + aux `vulnerability`, `compliance` |

### RAG 함수 추적 및 실패 시 동작

1. `/api/chat` → `_build_rag_context(mode, last_user)`가 템플릿을 고른다. 알 수 없는 모드는 general 템플릿으로 fallback한다.
2. `_hyde()`가 기본 `CHAT_MODEL`로 가상 문서를 생성한다. 실패 시 원본 질문을 사용한다. 요청에서 선택한 응답 모델과 HyDE 모델은 다를 수 있다.
3. `_build_rag_context()`가 모드별 검색 coroutine을 `asyncio.gather(return_exceptions=True)`로 병렬 실행한다.
   - `notes`: HyDE 문서로 `_qdrant_search_hybrid(..., limit=10)`.
   - `ctf_payloads`: 원본 질문으로 hybrid, limit=5.
   - hybrid: `_embed()`(nomic dense) + `_sparse_encode()`(**BM25**, BM42 아님), Qdrant named dense/sparse prefetch 각 limit×2 → RRF fusion. Sparse 실패 시 dense, query API 실패 시 legacy named-dense search.
   - `security_kb`: `_qdrant_search_security_kb()`로 **unnamed dense-only** 검색. HyDE main limit=12, 원문이 다를 때 같은 main category 원본 검색 limit=6, aux 각각 limit=6. category=None은 전체이며 정확한 category 문자열로 필터한다.
4. 검색 결과를 합쳐 `_rerank_flashrank(query, all_hits, 8)`를 thread에서 실행한다. 결과 dictionary의 `id`로 입력 청크를 재배열한다. passage 내용은 500자로 제한한다. 초기화/실행 실패 시 입력 순서 상위 8개를 사용한다. 후보가 8개 이하라도 재정렬한다.
5. **상위 8개 선택 후** `note_id`(없으면 title) 중복을 제거한다. 추가 후보를 채우지 않으므로 최종 출처는 최대 8개다.
6. 출처 metadata의 score는 FlashRank 점수로 덮어쓰지 않은 **원래 검색 점수**다. hybrid RRF와 KB cosine은 같은 확률 척도가 아니다. 결과가 3개 미만 또는 첫 score<0.3이면 `rag_gaps`에 기록한다.
7. 청크 내용이 비면 검색 payload의 파일 경로 읽기를 시도한다(최대 2000자). 각 컨텍스트는 1500자로 제한하고 `{context}`를 치환한다. 결과가 없으면 빈 컨텍스트로 치환한다.

자동 RAG와 tool 호출은 별개다. `TOOLS_BY_MODE`는 general/rag/ctf/pentest/bypass/threat만 명시하고, remediator/diagnostics는 스트리머의 general 도구 fallback을 사용한다. CTF의 `ctf_payloads` 내용과 bypass 프롬프트 문구는 사용자 검토 대상이다. 수동 OWASP Top10 색인기의 `owasp_top10`은 `owasp`와 다른 category이며 필터를 통합하거나 콘텐츠를 변경하지 않았다.

---

## 5. 2nd Brain 페이지 (`/brain`)

### 5.1 탭 구성

- **노트**: 좌측 도메인 필터 + 노트 목록 + marked.js 마크다운 뷰어
- **업로드**: drag-and-drop → `/api/brain/upload` → inbox → pipeline 처리
  - 업로드 탭이 열려 있는 동안 `loadJobs()`의 finally에서 `setTimeout(loadJobs, 3000)`으로 갱신한다. 빈 목록·HTTP 실패·네트워크 오류에서도 재시도하며 탭 전환 시 타이머를 해제한다.
  - 상태는 `pending/processing/completed/failed/skipped`; failed 표시를 실제 DB 상태명에 맞춘다.
- **연결도**: D3.js force-directed graph — wikilink `[[노트명]]` 기반 엣지; `[[노트명|별칭]]`, `[[노트명#섹션]]`도 파싱한다. DB title과 일치하는 대상만 연결하며 중복/자기 연결은 제외한다.
- 노트 목록은 현재 UI에서 최대 300개, 무필터 그래프는 API에서 최대 500개다. 도메인 필터 그래프에는 500개 제한이 없다. UI에 추가 페이지 탐색은 구현되지 않았다.
- Markdown 뷰어는 marked 출력의 HTML을 DOMPurify로 정제하고 목록/메타데이터 문자열은 HTML escape한다. 렌더링 라이브러리는 CDN이므로 브라우저 네트워크 접근이 필요하다.

### 5.2 도메인 색상 맵

아래 8개 값은 `brain.html`의 `DOMAIN_COLORS`와 일치한다. 미등록 도메인(한국어 pipeline 도메인 포함)은 `#888`이다. `index.html`에는 이 8도메인 맵이 없고 general/rag/ctf/remediator/diagnostics **모드 chip** 색상이 별도로 정의되어 있다.

| 도메인 | 색상 |
|--------|------|
| vulnerability | `#c05050` |
| owasp | `#e07c3a` |
| pentest | `#d4a030` |
| advisory | `#3aaa76` |
| compliance | `#5b94d8` |
| threat_intel | `#9b5fd4` |
| vuln_env | `#46b0b0` |
| general | `#888` |

---

## 6. 데이터베이스 스키마

### PostgreSQL — `dolphin_brain`

`services/pipeline/src/db.py`의 SQLAlchemy `Base.metadata.create_all()`이 실제 테이블을 생성한다. 아래는 모델의 전체 컬럼을 PostgreSQL 타입으로 1:1 옮긴 목록이다. `Column(default=...)`는 ORM 클라이언트 기본값이며 SQL `DEFAULT`와 구별한다. `_migrate()`는 기존 DB에 file_hash와 검증 컬럼을 추가하고 gap_log를 생성한다. gap_log의 수동 CREATE TABLE 경로에는 created_at `DEFAULT now()`가 있다.

**`processing_jobs`**

| 컬럼 | 실제 SQL 타입 | 제약/기본값 및 의미 |
|------|---------------|----------------------|
| id | SERIAL | PRIMARY KEY; NOT NULL |
| source_path | VARCHAR(1024) | NOT NULL |
| source_type | VARCHAR(16) | NULL 허용; file/folder |
| domain | VARCHAR(64) | NULL 허용 |
| status | VARCHAR(16) | NULL 허용; ORM default: 'pending'; pending/processing/completed/failed/skipped |
| created_notes | TEXT | NULL 허용; 생성 경로 JSON 배열 문자열 (TEXT[]) 아님 |
| storage_path | VARCHAR(1024) | NULL 허용 |
| error_message | TEXT | NULL 허용 |
| file_hash | VARCHAR(32) | NULL 허용; 원본 내용 MD5, 32자리 hex |
| created_at | TIMESTAMP WITHOUT TIME ZONE | NULL 허용; ORM default: datetime.utcnow; UTC 생성 시각 |
| completed_at | TIMESTAMP WITHOUT TIME ZONE | NULL 허용; completed/failed/skipped 종결 시각 |

**`note_index`**

| 컬럼 | 실제 SQL 타입 | 제약/기본값 및 의미 |
|------|---------------|----------------------|
| id | SERIAL | PRIMARY KEY; NOT NULL |
| note_path | VARCHAR(1024) | UNIQUE; NOT NULL; 현재 생성 코드는 /vault/... 절대 경로; 상대 경로 읽기도 지원 |
| title | VARCHAR(512) | NULL 허용 |
| domain | VARCHAR(64) | NULL 허용 |
| tags | TEXT | NULL 허용; JSON 배열 문자열 (TEXT[]) 아님 |
| source_file | VARCHAR(1024) | NULL 허용 |
| qdrant_id | VARCHAR(64) | NULL 허용 |
| created_at | TIMESTAMP WITHOUT TIME ZONE | NULL 허용; ORM default: datetime.utcnow; UTC 생성 시각 |
| updated_at | TIMESTAMP WITHOUT TIME ZONE | NULL 허용; ORM default: datetime.utcnow; upsert 갱신 시각 |
| validated_at | TIMESTAMP WITHOUT TIME ZONE | NULL 허용 |
| validation_verdict | VARCHAR(32) | NULL 허용 |
| reprocess_count | INTEGER | NULL 허용; ORM default: 0 |

**`gap_log`**

| 컬럼 | 실제 SQL 타입 | 제약/기본값 및 의미 |
|------|---------------|----------------------|
| id | SERIAL | PRIMARY KEY; NOT NULL |
| domain | VARCHAR(64) | NOT NULL |
| topic | VARCHAR(512) | NOT NULL |
| output_path | VARCHAR(1024) | NULL 허용 |
| created_at | TIMESTAMP WITHOUT TIME ZONE | NULL 허용; ORM default: datetime.utcnow; UTC 생성 시각 |

**`validation_log`**

| 컬럼 | 실제 SQL 타입 | 제약/기본값 및 의미 |
|------|---------------|----------------------|
| id | SERIAL | PRIMARY KEY; NOT NULL |
| note_path | VARCHAR(1024) | NOT NULL; 현재 생성 코드는 /vault/... 절대 경로; 상대 경로 읽기도 지원 |
| note_title | VARCHAR(512) | NULL 허용 |
| verdict | VARCHAR(32) | NULL 허용 |
| reason | TEXT | NULL 허용 |
| correction_applied | BOOLEAN | NULL 허용; ORM default: False |
| perplexity_used | BOOLEAN | NULL 허용; ORM default: False |
| claude_used | BOOLEAN | NULL 허용; ORM default: False |
| output_path | VARCHAR(1024) | NULL 허용 |
| created_at | TIMESTAMP WITHOUT TIME ZONE | NULL 허용; ORM default: datetime.utcnow; UTC 생성 시각 |

### chat 초기화 테이블 (`app.py:init_db`)

- `chat_sessions`: id UUID PK DEFAULT gen_random_uuid(), title TEXT NOT NULL DEFAULT '새 대화', mode TEXT NOT NULL DEFAULT 'general', model TEXT, created_at/updated_at TIMESTAMPTZ DEFAULT NOW(), owner_id TEXT(ALTER 추가).
- `chat_messages`: id UUID PK DEFAULT gen_random_uuid(), session_id UUID NOT NULL FK chat_sessions(id) ON DELETE CASCADE, role/content TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW(). session_id/created_at 복합 인덱스와 content의 simple tsvector GIN 인덱스.
- `rag_gaps`: id UUID PK DEFAULT gen_random_uuid(), query/mode TEXT NOT NULL, sources_count INT DEFAULT 0, top_score FLOAT, session_id UUID(FK 없음), created_at TIMESTAMPTZ DEFAULT NOW(). created_at과 mode/created_at 인덱스.

실제 실행 DB의 스키마 드리프트는 이번 소스 감사에서 접속 검증하지 않았다. API는 tags/created_notes의 JSON 문자열을 파싱해 배열로 반환한다.

---

## 7. 환경 변수 (`.env.example` 기준)

`.env.example`의 변수는 아래 **6개 전부**이며 표에만 있는 변수는 없다.

| 변수 | 설명 / 예제 기본값 |
|------|---------------------|
| `VAULT_PATH` | 호스트 vault 경로; 예제 placeholder `/path/to/your/obsidian/vault`, 실제 경로는 사용자가 설정 |
| `POSTGRES_PASSWORD` | 예제 placeholder `change_me_secure_password`; 실제 비밀번호는 사용자 설정 |
| `CHAT_MODEL` | `hf.co/mradermacher/Qwen2.5-Coder-14B-Instruct-Uncensored-GGUF:Q4_K_M` |
| `PERPLEXITY_API_KEY` | 기본 빈 값; 외부 검색/검증 보강만 건너뜀, 로컬 gap 분석 전체를 끄지 않음 |
| `ANTHROPIC_API_KEY` | 기본 빈 값; 외부 Claude 검토만 건너뜀, 로컬 검증 전체를 끄지 않음 |
| `DOCKER_PLATFORM` | `linux/arm64`; Compose platform의 기본값도 arm64 |

### Compose environment 전체 목록

다음은 네 서비스 environment 블록의 모든 줄이다. 서비스 URL, 컨테이너 경로, DB 이름/사용자, 타임존, 숫자 옵션은 리터럴 설정이며 비밀/추론 모델명이 아니다. 비밀번호/API 키/CHAT_MODEL은 `${VAR}` 치환이다.

**qdrant**

```yaml
QDRANT__OPTIMIZER__CPU_BUDGET: 1
QDRANT__OPTIMIZER__INDEXING_THRESHOLD: 50000
```

**postgres**

```yaml
POSTGRES_DB: dolphin_brain
POSTGRES_USER: dolphin
POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
```

**pipeline**

```yaml
OLLAMA_URL=http://host.docker.internal:11434
QDRANT_URL=http://qdrant:6333
POSTGRES_URL=postgresql://dolphin:${POSTGRES_PASSWORD}@postgres:5432/dolphin_brain
VAULT_PATH=/vault
STORAGE_PATH=/storage
INBOX_PATH=/storage/inbox
CONFIG_PATH=/config
TZ=Asia/Seoul
PERPLEXITY_API_KEY=${PERPLEXITY_API_KEY}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
PYTHONUNBUFFERED=1
```

**chat**

```yaml
OLLAMA_URL=http://host.docker.internal:11434
QDRANT_URL=http://qdrant:6333
POSTGRES_URL=postgresql://dolphin:${POSTGRES_PASSWORD}@postgres:5432/dolphin_brain
CHAT_MODEL=${CHAT_MODEL}
VAULT_PATH=/vault
STORAGE_PATH=/storage
PERPLEXITY_API_KEY=${PERPLEXITY_API_KEY}
FASTEMBED_CACHE_PATH=/fastembed_cache
TZ=Asia/Seoul
PYTHONUNBUFFERED=1
```

### 소스의 환경변수 읽기 / 모델 fallback

- `app.py`: OLLAMA_URL/QDRANT_URL/VAULT_PATH/STORAGE_PATH는 컨테이너 기본 URL/경로, POSTGRES_URL/PERPLEXITY_API_KEY는 빈 문자열, FASTEMBED_CACHE_PATH는 None. CHAT_MODEL에는 위 Qwen GGUF **모델명 fallback 리터럴**이 있다. 실제 API 키/비밀번호 fallback은 없다.
- `config.py:Config.load`: OLLAMA_URL/QDRANT_URL/VAULT_PATH/STORAGE_PATH/INBOX_PATH/CONFIG_PATH의 URL/경로 기본값과 POSTGRES_URL/PERPLEXITY_API_KEY/ANTHROPIC_API_KEY의 빈 문자열. 환경변수 읽기 자체에는 모델명/비밀값 fallback이 없다. 단 settings 프로퍼티에는 Qwen 모델, `llava:7b`, `nomic-embed-text`, `sonar-pro`, `claude-sonnet-4-6` 모델 기본값이 있다.
- `config/settings.yaml`은 pipeline 추론/vision/검증 모델을 설정한다. `llm.embedding_model: nomic-embed-cpu`가 있어도 현재 `LLMClient.embed()`는 FastEmbed `nomic-ai/nomic-embed-text-v1`을 직접 사용한다(설정 프로퍼티와 실제 backend 구분).
- `perplexity.py`와 `claude_client.py` 생성자에는 sonar-pro/claude-sonnet-4-6 기본값이 있다. chat 웹 검색 도구는 sonar-pro를 직접 선택한다. dense/sparse/rerank 모델명 역시 코드 상수다. 설정 기본값이지 노출된 비밀값은 아니므로 임의 제거하지 않았다.
- 수동 indexer는 QDRANT_URL/OLLAMA_URL, GUIDELINE_DIR/REPORT_DIR/DATA_DIR, CHECKPOINT_FILE, OWASP indexer의 COLLECTION/FASTEMBED_CACHE_PATH 등을 추가로 읽는다. 이는 `.env.example`의 사용자 변수와 별개이며 Compose 자동 전달 항목이 아니다.

**로컬 설정 이슈:** 사용자 `.env`의 VAULT_PATH가 비어 `docker compose config`가 실패한다는 사전 확인 결과는 그대로 유지한다. 실제 경로를 지어 넣거나 로컬 `.env`를 변경하지 않았다. 이번 감사에서 그 실패를 재실행하지 않았다.

---

## 8. 파일 구조

현재 소스/설정/도구 목록이다. `.env`, data, Storage 내용과 가상환경/캐시/로그는 로컬 실행 자료이므로 파일 전체를 전개하지 않는다.

```
dolphin_ai/
├── docker-compose.yml
├── .env                         # 로컬 설정; 커밋 금지
├── .env.example
├── .gitignore
├── README.md
├── Makefile
├── trigger_pdf_processing.sh
├── Storage/
│   └── inbox/                    # 입력 (ideas/archive_localllm 하위 폴더 지원)
├── data/
│   ├── qdrant/
│   ├── postgres/
│   └── fastembed/
├── config/
│   ├── settings.yaml             # 모델/처리/폴더/검증/gap 설정
│   └── prompts.yaml              # pipeline 작업별 프롬프트 템플릿
├── docs/
│   ├── dev-spec.md
│   └── audit-report.md            # 감사 발견사항/수정 근거/검증 범위
├── tests/
│   ├── test_audit.py              # 외부 서비스 없는 회귀 검증
│   └── brain_polling.test.js      # 업로드 폴링 JS 회귀 검증
├── tools/
│   ├── artex_session_manager.py   # ARTEX API 조회/별도 노트 기록 (감사 수정 제외)
│   ├── pentest_rag_query.py       # RAG 질의 도구
│   ├── doc2md.py
│   └── doc2md.sh
└── services/
    ├── chat/
    │   ├── app.py
    │   ├── requirements.txt
    │   ├── Dockerfile
    │   └── static/
    │       ├── index.html
    │       └── brain.html
    └── pipeline/
        ├── requirements.txt
        ├── Dockerfile
        └── src/
            ├── __init__.py               # src 패키지 표시 (빈 파일)
            ├── activity.py               # 작업/Health Check를 새 이벤트 Markdown 파일로 기록
            ├── claude_client.py          # 검증 최후 단계 Anthropic Messages API 클라이언트
            ├── config.py                 # 환경변수/YAML 로드와 경로·설정 프로퍼티
            ├── db.py                     # ORM 테이블 생성/마이그레이션 및 작업·노트·검증·gap CRUD
            ├── gap_analyzer.py           # 도메인별 누락 주제 탐지와 새 개념서 생성
            ├── ideas_organizer.py        # 유사 폴더 후보 분석; 기존 vault 이동은 차단
            ├── index_guidelines.py       # 진단 가이드라인을 security_kb에 수동 색인
            ├── index_owasp.py            # OWASP Top10 HTTP 수집 후 security_kb에 수동 색인
            ├── index_reports.py          # 진단 보고서를 security_reports에 수동 색인
            ├── index_source_collector.py # 수집 raw/github·raw/web 문서를 security_kb에 수동 색인
            ├── linker.py                 # notes 청킹/벡터 색인·검색·생성 전 링크 제안; 기존 편집 차단
            ├── llm.py                    # Ollama 추론과 FastEmbed 임베딩, 분류·요약·검증·gap 텍스트 생성
            ├── main.py                   # 초기화/기존 inbox 처리/감시 및 busy·idle 상태 관리
            ├── moc.py                    # 도메인 MOC를 기존 파일 보존하는 새 스냅샷으로 생성
            ├── mover.py                  # inbox 원본을 Storage로 충돌 회피 이동; vault 이동 차단
            ├── new_files.py              # 배타적 새 파일/파일 그룹 생성과 번호 충돌 회피
            ├── parser.py                 # 문서·코드·이미지·SQLite 텍스트 추출/변환
            ├── perplexity.py             # Sonar API 검색과 출처 URL 본문 수집
            ├── processor.py              # 파싱→분류→요약→새 노트/링크→DB→Storage→MOC/활동 흐름
            ├── reindex_worker.py         # 별도 프로세스 실행용 notes 누락 색인 worker
            ├── relink_isolated.py        # 고립 노트 판별 helper; 기존 노트 재링크 실행 차단
            ├── relink_worker.py          # legacy 재링크 worker; 현재 정책상 0건/경고 반환
            ├── scheduler.py              # 자정/처리 후 백필·Health Check·검증·gap 스케줄
            ├── validator.py              # 로컬 평가/외부 보강/Claude 단계 검증 및 새 보충·수정본
            ├── vault.py                  # frontmatter/Part 분할과 링크를 포함한 새 노트 생성
            └── watcher.py                # watchdog 이벤트 필터/debounce 및 처리 상태 관리
```

로컬의 `ORCHESTRATION_HANDOVER.md`는 인수인계 문서이며 `.gitignore` 대상이고 서비스가 읽는 소스가 아니다.

---

## 9. 운영 명령어

`.env`에 사용자가 실제 VAULT_PATH와 모델/비밀번호를 설정한 뒤 실행한다. 아래 명령은 예제 경로를 덮어씌우지 않는다.

```bash
# 전체 스택 시작
docker compose --env-file .env up -d

# chat 단독 리빌드
docker compose --env-file .env up -d --build --no-deps chat

# 로그 확인
docker logs dolphin-chat -f
docker logs dolphin-pipeline -f

# 파이프라인 테스트 (파일 inbox에 복사)
cp myfile.pdf ./Storage/inbox/
```

---

## 10. 제약사항 (개발 시 필수 준수)

- vault 기존 `.md`의 내용 수정·덮어쓰기·append·이동·삭제 금지. **새 파일 생성만 허용**한다.
- `new_files.write_new_file/write_new_files`는 `open("x")`로 기존 파일/동명 symlink를 덮어쓰지 않고 `_1`, `_2` suffix로 재시도한다. 그룹 생성 실패 시 이번 호출에서 배타적으로 만든 파일만 제거하며 기존 파일은 유지한다.
- 새 노트의 정제/frontmatter/링크는 최초 생성 본문에 포함한다. 기존 노트 재링크·백링크·정제·Ideas 이동은 실행 차단한다. DB/Qdrant 메타데이터 갱신과 읽기는 허용한다.
- MOC는 전체 링크 목록의 새 스냅샷, 활동 로그는 호출마다 새 이벤트 파일로 기록한다. 기존 `00_activity_log.md`에 append하지 않는다. 검증/gap 산출물도 동명 충돌을 회피한다.
- Compose pipeline의 vault는 신규 산출물을 위해 쓰기 가능, chat의 vault는 읽기 전용이다. 쓰기 가능 mount 자체가 기존 편집을 방지하는 것은 아니므로 코드의 create-only 경로와 이동 차단을 유지해야 한다. 수동 shell/외부 프로그램의 쓰기는 이 코드 정책의 보호 범위 밖이다.
- `mover.py`는 Storage로 inbox 원본을 이동하지만 vault 내부 원본/대상을 거부한다. 기본 INBOX_PATH는 `/storage/inbox`다.
- ARTEX/Ochestration 소스는 수정 금지, 발견사항만 보고한다. 저장소 검색에서 `tools/artex_session_manager.py`의 ARTEX HTTP GET 연동과 chat의 X-Orchestration 헤더를 확인했다. pipeline 소스에는 ARTEX/Ochestration 참조가 없다. 다른 repo 소스를 로드·수정하는 코드는 발견하지 않았다.
- `.env` git commit 금지. 이번 작업은 commit/history rewrite를 하지 않는다.

검증 결과와 실행 제약은 `docs/audit-report.md`를 참고한다. 소스/격리 회귀 검증과 실제 Docker/LLM/DB 실행 검증을 구분한다.
