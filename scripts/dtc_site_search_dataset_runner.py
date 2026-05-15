#!/usr/bin/env python3
"""Run the DTC site-search CSV as a concurrent self-evolution soak test.

Each completed row is written back to the CSV immediately:

- same_product: final agent response
- trajectory: compact JSON with session, metrics, tools, and tool events
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import shutil
import sys
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.dtc_site_search_ab import build_stateless_prompt, run_agent_prompt  # noqa: E402

ACTIVE_LIFECYCLE_STATUSES = {"building", "testing", "reviewing", "generating", "repairing"}


def _row_domain(row: Dict[str, str]) -> str:
    return (row.get("cleaned_domain") or row.get("clean_domain") or "").strip().lower()


def _trajectory(result: Dict[str, Any]) -> str:
    events = []
    for event in result.get("events") or []:
        if event.get("type") != "tool_complete":
            continue
        events.append({
            "name": event.get("name"),
            "tool_call_id": event.get("tool_call_id"),
            "args": event.get("args") or {},
            "preview": str(event.get("preview") or "")[:1200],
            "at": event.get("at"),
        })
    complete_names = [event["name"] for event in events]
    payload = {
        "session_id": result.get("session_id"),
        "elapsed_seconds": result.get("elapsed_seconds"),
        "api_calls": result.get("api_calls"),
        "input_tokens": result.get("input_tokens"),
        "output_tokens": result.get("output_tokens"),
        "total_tokens": result.get("total_tokens"),
        "tool_calls": complete_names,
        "used_generated_tool": "dtc_site_search_tool" in complete_names,
        "used_skill": "skill_view" in complete_names,
        "events": events,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _atomic_write_csv(path: Path, fieldnames: List[str], rows: List[Dict[str, str]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def _active_lifecycle_states() -> List[Dict[str, Any]]:
    states = []
    root = PROJECT_ROOT / "dtc_site_search_data"
    for raw_path in sorted(root.glob("*/raw/*.json")):
        cleaned_path = raw_path.parents[1] / "cleaned" / f"{raw_path.stem}.md"
        state_path = raw_path.parents[1] / "state.json"
        if not cleaned_path.exists() or not state_path.exists():
            states.append({
                "site_key": raw_path.parents[1].name,
                "pending_raw": raw_path.name,
                "skill_status": "raw_pending_cleanup",
                "tool_status": "",
            })
    for state_path in sorted(root.glob("*/state.json")):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        skill_status = str(state.get("skill_status") or "")
        tool_status = str(state.get("tool_status") or "")
        if skill_status in ACTIVE_LIFECYCLE_STATUSES or tool_status in ACTIVE_LIFECYCLE_STATUSES:
            states.append({
                "site_key": state_path.parent.name,
                "skill_status": skill_status,
                "tool_status": tool_status,
                "success_count": state.get("success_count"),
                "x_success_count": state.get("x_success_count"),
                "y_no_change_count": state.get("y_no_change_count"),
                "stable_skill_update_count": state.get("stable_skill_update_count"),
            })
    return states


def _wait_for_lifecycle_settle(timeout_seconds: int) -> List[Dict[str, Any]]:
    if timeout_seconds <= 0:
        return _active_lifecycle_states()
    deadline = time.time() + timeout_seconds
    active: List[Dict[str, Any]] = []
    while time.time() < deadline:
        active = _active_lifecycle_states()
        if not active:
            return []
        print(f"[settle] active_lifecycle={active}", flush=True)
        time.sleep(5)
    return _active_lifecycle_states()


def _run_row(index: int, row: Dict[str, str], max_iterations: int, model: str) -> Tuple[int, Dict[str, Any]]:
    prompt = build_stateless_prompt(row.get("prompt", ""))
    result = run_agent_prompt(
        prompt,
        session_prefix=f"dtc_dataset_{index}",
        max_iterations=max_iterations,
        model=model,
    )
    return index, result


def _next_ready_domain(
    queues: "OrderedDict[str, Deque[int]]",
    active_domains: set[str],
) -> Tuple[str, int] | Tuple[None, None]:
    for domain, queue in queues.items():
        if domain in active_domains or not queue:
            continue
        return domain, queue.popleft()
    return None, None


def _next_round_robin(
    queues: "OrderedDict[str, Deque[int]]",
    cursor: int,
) -> Tuple[str, int, int] | Tuple[None, None, int]:
    domains = list(queues.keys())
    if not domains:
        return None, None, cursor
    for offset in range(len(domains)):
        pos = (cursor + offset) % len(domains)
        domain = domains[pos]
        queue = queues.get(domain)
        if queue:
            return domain, queue.popleft(), (pos + 1) % len(domains)
    return None, None, cursor


def run_dataset(
    csv_path: Path,
    *,
    concurrency: int,
    max_iterations: int,
    model: str,
    limit: int,
    resume: bool,
    schedule: str,
    settle_seconds: int,
) -> Dict[str, Any]:
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]
    if "same_product" not in fieldnames:
        fieldnames.append("same_product")
    trajectory_col = " trajectory" if " trajectory" in fieldnames else "trajectory"
    if trajectory_col not in fieldnames:
        fieldnames.append(trajectory_col)

    backup = csv_path.with_suffix(csv_path.suffix + f".bak-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
    shutil.copy2(csv_path, backup)

    queues: "OrderedDict[str, Deque[int]]" = OrderedDict()
    selected = 0
    for idx, row in enumerate(rows):
        prompt = (row.get("prompt") or "").strip()
        domain = _row_domain(row)
        if not prompt or not domain:
            continue
        if resume and (row.get(trajectory_col) or "").strip():
            continue
        queues.setdefault(domain, deque()).append(idx)
        selected += 1
        if limit and selected >= limit:
            break

    started_at = time.time()
    completed = 0
    failed = 0
    active_domains: set[str] = set()
    futures: Dict[concurrent.futures.Future, str] = {}
    row_queue: Deque[int] = deque(idx for queue in queues.values() for idx in queue)
    round_robin_cursor = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        while True:
            while len(futures) < concurrency:
                if schedule == "domain_serial":
                    domain, idx = _next_ready_domain(queues, active_domains)
                    if domain is None or idx is None:
                        break
                    active_domains.add(domain)
                elif schedule == "round_robin":
                    domain, idx, round_robin_cursor = _next_round_robin(queues, round_robin_cursor)
                    if domain is None or idx is None:
                        break
                else:
                    if not row_queue:
                        break
                    idx = row_queue.popleft()
                    domain = _row_domain(rows[idx])
                active_domains.add(domain)
                futures[executor.submit(_run_row, idx, rows[idx], max_iterations, model)] = domain
                print(f"[start] row={idx} domain={domain}", flush=True)

            if not futures:
                break

            done, _ = concurrent.futures.wait(
                futures,
                timeout=5,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            if not done:
                continue
            for future in done:
                domain = futures.pop(future)
                active_domains.discard(domain)
                try:
                    idx, result = future.result()
                    rows[idx]["same_product"] = str(result.get("final_response") or "").strip()
                    rows[idx][trajectory_col] = _trajectory(result)
                    completed += 1
                    print(
                        f"[done] row={idx} domain={domain} tools="
                        f"{json.loads(rows[idx][trajectory_col]).get('tool_calls')} "
                        f"tokens={result.get('total_tokens')} elapsed={result.get('elapsed_seconds')}",
                        flush=True,
                    )
                except Exception as exc:
                    failed += 1
                    idx = -1
                    rows_text = json.dumps({"error": str(exc), "domain": domain}, ensure_ascii=False)
                    print(f"[fail] domain={domain} error={exc}", flush=True)
                    # The failing row was already popped. Record the failure in
                    # the first uncompleted row for that domain if possible.
                    for candidate_idx, row in enumerate(rows):
                        if _row_domain(row) == domain and not (row.get(trajectory_col) or "").strip():
                            idx = candidate_idx
                            row["same_product"] = ""
                            row[trajectory_col] = rows_text
                            break
                _atomic_write_csv(csv_path, fieldnames, rows)

    unsettled = _wait_for_lifecycle_settle(settle_seconds)

    return {
        "csv": str(csv_path),
        "backup": str(backup),
        "selected": selected,
        "completed": completed,
        "failed": failed,
        "schedule": schedule,
        "concurrency": concurrency,
        "unsettled_lifecycle": unsettled,
        "elapsed_seconds": round(time.time() - started_at, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default="hermes_test - Sheet1.csv")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--max-iterations", type=int, default=45)
    parser.add_argument("--model", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--settle-seconds", type=int, default=0)
    parser.add_argument(
        "--schedule",
        choices=("row_order", "round_robin", "domain_serial"),
        default="row_order",
        help=(
            "row_order allows same-domain concurrency from CSV order; "
            "round_robin mixes domains; domain_serial allows at most one active row per domain."
        ),
    )
    args = parser.parse_args()
    report = run_dataset(
        Path(args.csv),
        concurrency=args.concurrency,
        max_iterations=args.max_iterations,
        model=args.model,
        limit=args.limit,
        resume=args.resume,
        schedule=args.schedule,
        settle_seconds=args.settle_seconds,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
