#!/usr/bin/env python3
"""
ARTEX Session Manager — Note-Based Pentest Loop Engine

8b 컨텍스트 한계(8k) 극복을 위한 외부 노트 기반 상태 관리.
ARTEX 없이 독립 실행 가능. notes.md를 외부 메모리로 사용.

사용법:
  python3 artex_session_manager.py --target http://TARGET --notes /tmp/notes.md --checklist /tmp/checklist.txt
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


NOTES_TEMPLATE = """# Pentest Notes — {target}
Generated: {timestamp}

## DONE
<!-- [DONE] endpoint: result -->

## FINDINGS
<!-- [FINDING] severity: description -->
<!-- [FINDING] CRITICAL: SQLi POST /login — admin bypass confirmed -->

## TODO
{checklist}

## FAILED
<!-- [FAIL] endpoint: reason -->

## STATS
- Start: {timestamp}
- Rounds: 0
- Findings: 0
"""


def load_notes(notes_path: Path) -> str:
    if notes_path.exists():
        return notes_path.read_text()
    return ""


def save_notes(notes_path: Path, content: str):
    notes_path.write_text(content)


def parse_findings(notes_content: str) -> list[dict]:
    findings = []
    for line in notes_content.splitlines():
        line = line.strip()
        if line.startswith("[FINDING]"):
            # [FINDING] CRITICAL: desc
            parts = line[len("[FINDING]"):].strip().split(":", 1)
            if len(parts) == 2:
                findings.append({"severity": parts[0].strip(), "description": parts[1].strip()})
    return findings


def parse_done(notes_content: str) -> list[str]:
    done = []
    for line in notes_content.splitlines():
        line = line.strip()
        if line.startswith("[DONE]") or line.startswith("- [x]"):
            done.append(line)
    return done


def query_llm(model: str, base_url: str, prompt: str) -> str:
    """Ollama API 직접 호출"""
    import urllib.request
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    req = urllib.request.Request(
        f"{base_url}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
        return data["message"]["content"]


def run_pentest_round(
    target: str,
    notes_path: Path,
    model: str = "hermes3:8b-8k",
    base_url: str = "http://localhost:11434",
    round_num: int = 1,
) -> str:
    notes = load_notes(notes_path)

    prompt = f"""You are a penetration tester. Target: {target}

CURRENT NOTES (your external memory — read carefully):
---
{notes}
---

Rules:
1. Read the TODO section. Pick ONE unchecked item.
2. Write the curl command to test it.
3. Analyze the result.
4. Update the notes:
   - If found: add [FINDING] SEVERITY: description
   - If done: add [DONE] endpoint: result (move from TODO to DONE)
   - If failed: add [FAIL] endpoint: reason

Output format (STRICT):
CURL_CMD: <the exact curl command>
RESULT_SUMMARY: <one line>
NOTES_UPDATE: <lines to add to notes>
"""

    response = query_llm(model, base_url, prompt)
    return response


def update_notes_from_response(notes_path: Path, response: str, round_num: int):
    notes = load_notes(notes_path)
    lines = response.splitlines()

    notes_update_lines = []
    in_update = False
    for line in lines:
        if line.startswith("NOTES_UPDATE:"):
            in_update = True
            rest = line[len("NOTES_UPDATE:"):].strip()
            if rest:
                notes_update_lines.append(rest)
        elif in_update:
            notes_update_lines.append(line)

    if notes_update_lines:
        update_text = "\n".join(notes_update_lines)
        # DONE 섹션 업데이트
        for line in notes_update_lines:
            stripped = line.strip()
            if stripped.startswith("[DONE]"):
                notes = notes.replace("## DONE\n<!-- [DONE]", f"## DONE\n{stripped}\n<!-- [DONE]")
            elif stripped.startswith("[FINDING]"):
                notes = notes.replace("## FINDINGS\n<!-- [FINDING]", f"## FINDINGS\n{stripped}\n<!-- [FINDING]")
            elif stripped.startswith("[FAIL]"):
                notes = notes.replace("## FAILED\n<!-- [FAIL]", f"## FAILED\n{stripped}\n<!-- [FAIL]")

    # Stats 업데이트
    findings = parse_findings(notes)
    done = parse_done(notes)
    stats_line = f"- Rounds: {round_num}\n- Findings: {len(findings)}\n- Done: {len(done)}"
    import re
    notes = re.sub(r"- Rounds: \d+\n- Findings: \d+.*", stats_line, notes, flags=re.DOTALL)

    save_notes(notes_path, notes)


def init_notes(notes_path: Path, target: str, checklist_items: list[str]):
    checklist = "\n".join(f"- [ ] {item}" for item in checklist_items)
    content = NOTES_TEMPLATE.format(
        target=target,
        timestamp=datetime.now().isoformat(),
        checklist=checklist,
    )
    save_notes(notes_path, content)
    print(f"[init] Notes created: {notes_path}")


# Default checklists per known target type
CHECKLISTS = {
    "juiceshop": [
        "SQLi POST /rest/user/login — email field ' OR 1=1--",
        "BFLA GET /api/Users — no auth, full user list",
        "Reflected XSS GET /rest/products/search?q=<script>",
        "BOLA GET /rest/basket/{id} — try IDs 1,2,3 with other user token",
        "Sensitive Data /ftp/ — directory listing",
    ],
    "crapi": [
        "BOLA GET /identity/api/v2/vehicle/{vehicleId}/location — try other user vehicle IDs",
        "OTP Brute Force POST /identity/api/auth/v3/check-otp — iterate 0000-9999",
        "SSRF POST /community/api/v2/community/posts/validate_link — internal URL",
        "BFLA GET /workshop/api/mechanic — no auth",
        "BOLA GET /community/api/v2/community/posts/{id} — other user posts",
        "Excessive Data GET /identity/api/v2/vehicle/vehicles — internal fields",
    ],
    "nodegoat": [
        "NoSQL Injection POST /login — JSON body {username:{$ne:null},password:{$ne:null}}",
        "IDOR POST /benefits — userId=1 with other user session",
        "SSJI GET /allocations/{id}?threshold=0';while(true){}'",
        "Stored XSS POST /profile — firstName=javascript:alert(1)",
        "CSRF POST /profile — _csrf empty string",
        "Unvalidated Redirect GET /learn?url=http://evil.example.com",
        "SSRF GET /research?url=http://127.0.0.1:4000/&symbol=login",
    ],
    "webgoat": [
        "SQLi POST /WebGoat/SqlInjection/assignment5a — account=Smith' OR 1=1 --",
        "JWT alg:none GET /WebGoat/JWT/votings/login then POST with alg:none token",
        "Path Traversal POST /WebGoat/PathTraversal/profile-upload — fullName=../test",
        "IDOR GET /WebGoat/IDOR/profile/2342388 — tom session, bill userId",
    ],
}


def artex_fetch_tasks(artex_token: str, artex_url: str) -> dict:
    """GET /api/tasks → {active, tasks:[...]}"""
    import urllib.request
    req = urllib.request.Request(
        f"{artex_url}/api/tasks",
        headers={"Authorization": f"Bearer {artex_token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def artex_fetch_findings(artex_token: str, artex_url: str) -> list[dict]:
    """GET /api/exploration/findings → all findings"""
    import urllib.request
    req = urllib.request.Request(
        f"{artex_url}/api/exploration/findings",
        headers={"Authorization": f"Bearer {artex_token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def artex_finding_to_note_line(finding: dict) -> str:
    sev = finding.get("severity", "medium").upper()
    vulnclass = finding.get("vulnclass", "")
    summary = finding.get("summary", "")
    fid = finding.get("id", "?")
    desc = f"[{vulnclass}] {summary}" if vulnclass else summary
    return f"[FINDING] {sev}: #{fid} {desc}"


def artex_write_findings_to_notes(notes_path: Path, task_id: str, findings: list[dict], seen_ids: set) -> set:
    """Write new findings for task_id to notes.md. Returns updated seen_ids."""
    task_findings = [f for f in findings if str(f.get("task_id")) == str(task_id)]
    new_findings = [f for f in task_findings if str(f.get("id")) not in seen_ids]

    if not new_findings:
        return seen_ids

    notes = load_notes(notes_path)
    for finding in new_findings:
        line = artex_finding_to_note_line(finding)
        notes = notes.replace(
            "## FINDINGS\n<!-- [FINDING]",
            f"## FINDINGS\n{line}\n<!-- [FINDING]"
        )
        seen_ids.add(str(finding.get("id")))
        print(f"  [finding] {line}")

    # Update stats
    all_findings = parse_findings(notes)
    import re
    stats_match = re.search(r"- Rounds: \d+\n- Findings: \d+", notes)
    if stats_match:
        notes = notes[:stats_match.start()] + f"- Rounds: (artex)\n- Findings: {len(all_findings)}" + notes[stats_match.end():]

    save_notes(notes_path, notes)
    return seen_ids


def artex_init_notes_for_task(notes_path: Path, task_id: str, task_name: str, target: str, target_type: str | None):
    """Initialize notes.md for an ARTEX task if it doesn't exist."""
    if notes_path.exists():
        return
    checklist = CHECKLISTS.get(target_type, ["ARTEX autonomous exploration — no manual checklist"])
    content = NOTES_TEMPLATE.format(
        target=f"{target} [ARTEX task={task_id}: {task_name}]",
        timestamp=datetime.now().isoformat(),
        checklist="\n".join(f"- [ ] {item}" for item in checklist),
    )
    # Add ARTEX-specific header
    content = f"<!-- ARTEX task_id={task_id} -->\n" + content
    save_notes(notes_path, content)
    print(f"[artex] Notes init: {notes_path}")


def artex_monitor_loop(
    artex_token: str,
    artex_url: str,
    task_notes_map: dict,  # {task_id: notes_path}
    poll_interval: int = 30,
    timeout_minutes: int = 60,
):
    """
    Poll ARTEX tasks + findings, write to notes.md files.
    task_notes_map: {"6": Path("/tmp/nodegoat.md"), "7": Path("/tmp/webgoat.md")}
    """
    seen_finding_ids: dict[str, set] = {tid: set() for tid in task_notes_map}
    deadline = time.time() + timeout_minutes * 60
    completed = set()

    print(f"\n[artex-monitor] Watching tasks: {list(task_notes_map.keys())}")
    print(f"[artex-monitor] Poll interval: {poll_interval}s, Timeout: {timeout_minutes}min")
    print(f"[artex-monitor] Notes files: {list(task_notes_map.values())}")

    tick = 0
    while time.time() < deadline:
        tick += 1
        try:
            # Fetch all findings once per tick
            all_findings = artex_fetch_findings(artex_token, artex_url)

            # Fetch task statuses
            tasks_data = artex_fetch_tasks(artex_token, artex_url)
            tasks_by_id = {str(t["id"]): t for t in tasks_data.get("tasks", [])}

            for task_id, notes_path in task_notes_map.items():
                task = tasks_by_id.get(str(task_id), {})
                status = task.get("status", "unknown")
                findings_count = task.get("findings", {})
                total_new = sum(findings_count.values()) if isinstance(findings_count, dict) else 0

                if task_id in completed:
                    continue

                print(f"\n[tick {tick}] task={task_id} ({task.get('name','?')}) status={status} findings={findings_count}")

                # Write new findings to notes
                seen_finding_ids[task_id] = artex_write_findings_to_notes(
                    notes_path, task_id, all_findings, seen_finding_ids[task_id]
                )

                if status == "done":
                    completed.add(task_id)
                    print(f"  [artex-monitor] Task {task_id} DONE. Notes: {notes_path}")

            if len(completed) == len(task_notes_map):
                print("\n[artex-monitor] All tasks complete.")
                break

        except Exception as e:
            print(f"[artex-monitor] Error tick {tick}: {e}", file=sys.stderr)

        time.sleep(poll_interval)

    # Final findings dump
    print("\n[artex-monitor] Final notes dump:")
    for task_id, notes_path in task_notes_map.items():
        if notes_path.exists():
            print(f"\n=== {notes_path} ===")
            print(load_notes(notes_path))


def main():
    parser = argparse.ArgumentParser(description="ARTEX Note-Based Session Manager")
    parser.add_argument("--target", default=None)
    parser.add_argument("--notes", required=True, type=Path)
    parser.add_argument("--target-type", choices=list(CHECKLISTS.keys()), default=None)
    parser.add_argument("--max-rounds", type=int, default=20)
    parser.add_argument("--model", default="hermes3:8b-8k")
    parser.add_argument("--base-url", default="http://localhost:11434")
    parser.add_argument("--init-only", action="store_true")
    parser.add_argument("--show-notes", action="store_true")

    # ARTEX monitor mode
    parser.add_argument("--artex-token", default=None, help="ARTEX JWT token")
    parser.add_argument("--artex-url", default="http://127.0.0.1:8787", help="ARTEX base URL")
    parser.add_argument("--artex-task-id", action="append", dest="artex_task_ids",
                        metavar="ID:NOTES_PATH", help="task_id:notes_path pairs (repeatable)")
    parser.add_argument("--artex-poll", type=int, default=30, help="Poll interval seconds")
    parser.add_argument("--artex-timeout", type=int, default=60, help="Monitor timeout minutes")

    args = parser.parse_args()

    # ARTEX monitor mode
    if args.artex_token and args.artex_task_ids:
        task_notes_map = {}
        for item in args.artex_task_ids:
            task_id, notes_file = item.split(":", 1)
            np = Path(notes_file)
            target_type = args.target_type
            # Init notes if not present
            artex_init_notes_for_task(np, task_id, f"task-{task_id}", notes_file, target_type)
            task_notes_map[task_id] = np

        artex_monitor_loop(
            artex_token=args.artex_token,
            artex_url=args.artex_url,
            task_notes_map=task_notes_map,
            poll_interval=args.artex_poll,
            timeout_minutes=args.artex_timeout,
        )
        return

    # Standalone mode requires --target
    if not args.target:
        print("Error: --target required in standalone mode", file=sys.stderr)
        sys.exit(1)

    # Init notes
    if not args.notes.exists() or args.init_only:
        checklist = CHECKLISTS.get(args.target_type, ["Manual checklist required"])
        init_notes(args.notes, args.target, checklist)
        if args.init_only:
            return

    if args.show_notes:
        print(load_notes(args.notes))
        return

    print(f"[start] Target: {args.target}, Model: {args.model}, Max rounds: {args.max_rounds}")
    print(f"[notes] {args.notes}")

    for round_num in range(1, args.max_rounds + 1):
        print(f"\n[round {round_num}] Starting...")
        notes = load_notes(args.notes)
        todo_count = notes.count("- [ ]")
        done_count = notes.count("- [x]") + notes.count("[DONE]")

        print(f"  TODO: {todo_count}, DONE: {done_count}, Findings: {len(parse_findings(notes))}")

        if todo_count == 0:
            print("[done] All checklist items completed.")
            break

        try:
            response = run_pentest_round(args.target, args.notes, args.model, args.base_url, round_num)
            print(f"[round {round_num}] LLM response received ({len(response)} chars)")

            for line in response.splitlines():
                if line.startswith("CURL_CMD:"):
                    print(f"  CMD: {line[9:].strip()[:80]}...")
                elif line.startswith("RESULT_SUMMARY:"):
                    print(f"  RESULT: {line[15:].strip()}")

            update_notes_from_response(args.notes, response, round_num)

        except Exception as e:
            print(f"[error] Round {round_num}: {e}", file=sys.stderr)
            time.sleep(5)

        time.sleep(2)

    print("\n[final] Session complete.")
    print(load_notes(args.notes))


def rag_lookup(finding_summary: str) -> list[dict]:
    qdrant_url      = os.environ.get("QDRANT_URL",          "http://localhost:6333")
    fastembed_cache = os.environ.get("FASTEMBED_CACHE_PATH", None)
    collection      = os.environ.get("COLLECTION",           "security_kb")
    try:
        from fastembed import TextEmbedding
        from qdrant_client import QdrantClient
        kwargs = {"model_name": "nomic-ai/nomic-embed-text-v1"}
        if fastembed_cache:
            kwargs["cache_dir"] = fastembed_cache
        embedder = TextEmbedding(**kwargs)
        vector = list(embedder.embed([finding_summary]))[0].tolist()
        client = QdrantClient(url=qdrant_url, timeout=15)
        hits = client.search(
            collection_name=collection,
            query_vector=vector,
            limit=3,
            with_payload=True,
        )
        return [
            {
                "score":    float(h.score),
                "category": h.payload.get("category", ""),
                "source":   h.payload.get("source", ""),
                "text":     h.payload.get("text", ""),
            }
            for h in hits
        ]
    except Exception:
        return []


def artex_finding_with_remediation(finding: dict) -> str:
    summary = finding.get("summary", "")
    results = rag_lookup(summary)
    if not results:
        return summary
    top_text = results[0]["text"][:500]
    return f"{summary}\n\n대응방안:\n{top_text}"


if __name__ == "__main__":
    main()
