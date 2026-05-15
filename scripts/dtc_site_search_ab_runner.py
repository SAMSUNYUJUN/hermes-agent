#!/usr/bin/env python3
"""Run real DTC site-search A/B tests from a CSV dataset.

The runner can execute locally or submit cases to a running Hermes API server
with --api-url so the A/B agents are created inside the same Hermes runtime as
production. It is intended for rollout validation, not unit tests:

- candidate: normal production path, uses published generated tool first.
- baseline_skill: explicitly disables generated tool use and falls back to the
  published site skill.

Example:

    python scripts/dtc_site_search_ab_runner.py \
      --csv "hermes_test - Sheet1.csv" \
      --domain revolve.com \
      --limit 3 \
      --concurrency 2 \
      --modes candidate,baseline_skill \
      --output /tmp/revolve_ab.json
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.dtc_site_search_ab import (  # noqa: E402
    DtcSiteSearchAbCase,
    build_mode_prompt,
    run_ab_cases,
    score_response,
)


URL_RE = re.compile(r"https?://[^\s<>)\"']+", re.IGNORECASE)


@dataclass
class AbCase(DtcSiteSearchAbCase):
    index: int
    sku_id: str
    domain: str
    prompt: str
    expected: str
    product_id: str = ""


def _norm_url(url: str) -> str:
    url = (url or "").strip().rstrip(".,)")
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.scheme:
        parsed = urlparse("https://" + url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/")
    return urlunparse(("https", host, path, "", "", ""))


def _extract_urls(text: str) -> List[str]:
    seen = set()
    urls: List[str] = []
    for match in URL_RE.findall(text or ""):
        norm = _norm_url(match)
        if norm and norm not in seen:
            seen.add(norm)
            urls.append(norm)
    return urls


def _expected_status(expected: str) -> str:
    value = (expected or "").strip()
    if not value:
        return "unknown"
    if value in {"无", "none", "None", "NO", "no", "not found"}:
        return "none"
    return "url"


def _score_response(final_response: str, expected: str) -> Dict[str, Any]:
    return score_response(final_response, expected)


def load_cases(
    csv_path: Path,
    domain: str = "",
    limit: int = 0,
    require_expected: bool = False,
    expected_url_only: bool = False,
) -> List[AbCase]:
    domain_norm = (domain or "").strip().lower()
    cases: List[AbCase] = []
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for idx, row in enumerate(reader):
            row_domain = (row.get("cleaned_domain") or row.get("clean_domain") or "").strip().lower()
            if domain_norm and row_domain != domain_norm:
                continue
            prompt = (row.get("prompt") or "").strip()
            sku_id = (row.get("sku_id") or "").strip()
            expected = (row.get("same_product") or "").strip()
            if not prompt:
                continue
            if require_expected and not expected:
                continue
            if expected_url_only and _expected_status(expected) != "url":
                continue
            cases.append(
                AbCase(
                    index=idx,
                    sku_id=sku_id,
                    domain=row_domain,
                    prompt=prompt,
                    expected=expected,
                    product_id=(row.get("product_id") or "").strip(),
                )
            )
            if limit and len(cases) >= limit:
                break
    return cases


def _mode_prompt(case: AbCase, mode: str) -> str:
    return build_mode_prompt(case, mode)


def run(cases: Iterable[AbCase], modes: List[str], concurrency: int, max_iterations: int, model: str) -> Dict[str, Any]:
    return run_ab_cases(cases, modes, concurrency, max_iterations, model)


def run_via_api(
    api_url: str,
    token: str,
    cases: List[AbCase],
    modes: List[str],
    concurrency: int,
    max_iterations: int,
    model: str,
) -> Dict[str, Any]:
    endpoint = api_url.rstrip("/") + "/api/dtc-site-search/ab"
    payload = {
        "cases": [case.__dict__ for case in cases],
        "modes": modes,
        "concurrency": concurrency,
        "max_iterations": max_iterations,
        "model": model,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Hermes-Session"] = token
    req = Request(endpoint, data=data, headers=headers, method="POST")
    with urlopen(req, timeout=max(60, max_iterations * 20)) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default="hermes_test - Sheet1.csv", help="CSV path with prompt/cleaned_domain/same_product columns")
    parser.add_argument("--domain", default="", help="Filter cleaned_domain, e.g. hsn.com or revolve.com")
    parser.add_argument("--limit", type=int, default=1, help="Maximum cases after filtering")
    parser.add_argument("--require-expected", action="store_true", help="Only select rows with same_product labels")
    parser.add_argument("--expected-url-only", action="store_true", help="Only select rows whose same_product is a URL")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--modes", default="candidate,baseline_skill")
    parser.add_argument("--max-iterations", type=int, default=30)
    parser.add_argument("--model", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--api-url", default="", help="Hermes dashboard/API base URL. When set, A/B runs inside that Hermes process.")
    parser.add_argument("--api-token", default="", help="Dashboard X-Hermes-Session token for --api-url")
    parser.add_argument("--list-cases", action="store_true", help="Only print selected cases")
    args = parser.parse_args()

    cases = load_cases(Path(args.csv), args.domain, args.limit, args.require_expected, args.expected_url_only)
    if args.list_cases:
        print(json.dumps([case.__dict__ for case in cases], ensure_ascii=False, indent=2))
        return 0
    if not cases:
        raise SystemExit("No cases selected")
    modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]
    if args.api_url:
        report = run_via_api(args.api_url, args.api_token, cases, modes, args.concurrency, args.max_iterations, args.model)
    else:
        report = run(cases, modes, args.concurrency, args.max_iterations, args.model)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
