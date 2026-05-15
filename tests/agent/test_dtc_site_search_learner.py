import json
import threading


def test_dtc_site_search_window_defaults(monkeypatch):
    from agent import dtc_site_search_learner as learner

    monkeypatch.delenv("HERMES_DTC_SITE_SEARCH_SUCCESS_THRESHOLD", raising=False)
    monkeypatch.delenv("HERMES_DTC_SITE_SEARCH_SKILL_UPDATE_WINDOW", raising=False)
    monkeypatch.delenv("HERMES_DTC_SITE_SEARCH_TOOL_GENERATION_STABLE_UPDATES", raising=False)
    monkeypatch.setattr(learner, "load_config", None, raising=False)

    assert learner.get_success_threshold() == 2
    assert learner.get_skill_update_window() == 1
    assert learner.get_tool_generation_stable_updates() == 2


def test_www_site_urls_canonicalize_to_bare_domain():
    from agent import dtc_site_search_learner as learner

    assert learner.normalize_site_url("https://www.hsn.com") == "https://hsn.com"
    assert learner.normalize_site_url("https://www.hsn.com/products/foo/123?x=1") == "https://hsn.com/products/foo/123"
    assert learner.site_key("https://www.hsn.com") == learner.site_key("https://hsn.com")
    assert learner.site_key("https://www.hsn.com/products/foo/123") == learner.site_key("https://hsn.com/products/foo/123")
    assert learner.site_key("https://www.hsn.com/products/foo/123") != learner.site_key("https://hsn.com")
    assert learner.skill_name_for_site("https://www.hsn.com") == learner.skill_name_for_site("https://hsn.com")


def test_legacy_site_state_merges_into_canonical_state(tmp_path, monkeypatch):
    from agent import dtc_site_search_learner as learner

    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", str(tmp_path / "data"))

    canonical = "https://hsn.com"
    legacy_exact = "https://www.hsn.com"
    legacy_path_exact = "https://www.hsn.com/products/foo/123"
    legacy_key = learner._site_key_from_normalized_exact(legacy_exact)
    legacy_path_key = learner._site_key_from_normalized_exact(legacy_path_exact)
    legacy_dir = tmp_path / "data" / legacy_key
    legacy_path_dir = tmp_path / "data" / legacy_path_key
    legacy_cleaned = legacy_dir / "cleaned" / "legacy.md"
    legacy_cleaned.parent.mkdir(parents=True)
    legacy_cleaned.write_text("legacy", encoding="utf-8")
    learner._write_json(
        legacy_dir / "state.json",
        {
            "site_url": legacy_exact,
            "records": [
                {
                    "record_id": "legacy",
                    "success": True,
                    "cleaned_path": str(legacy_cleaned),
                    "created_at": "1",
                }
            ],
        },
    )
    learner._write_json(
        legacy_path_dir / "state.json",
        {
            "site_url": legacy_path_exact,
            "records": [
                {
                    "record_id": "legacy-path",
                    "success": True,
                    "cleaned_path": "legacy-path.md",
                    "created_at": "3",
                }
            ],
        },
    )
    learner._write_json(
        learner._site_dir(canonical) / "state.json",
        {
            "site_url": canonical,
            "records": [
                {
                    "record_id": "canonical",
                    "success": True,
                    "cleaned_path": "canonical.md",
                    "created_at": "2",
                }
            ],
        },
    )

    state = learner._merge_legacy_site_state("https://www.hsn.com")

    assert state["site_url"] == canonical
    assert state["success_count"] == 2
    assert legacy_key in state["merged_legacy_site_keys"]
    assert legacy_path_key not in state["merged_legacy_site_keys"]
    assert state["merged_legacy_www_site_key"] == legacy_key
    assert [r["record_id"] for r in state["records"]] == ["legacy", "canonical"]


def test_dtc_site_search_context_reports_generated_tool(tmp_path, monkeypatch):
    from agent import dtc_site_search_learner as learner

    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_SKILL_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_TOOL_GENERATION_STABLE_UPDATES", "3")

    site_url = "https://example.com"
    name = learner.skill_name_for_site(site_url)
    skill_md = learner._skill_dir(name) / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("---\nname: test\n---\n", encoding="utf-8")

    script = learner._generated_tool_path(site_url)
    script.parent.mkdir(parents=True)
    script.write_text("console.log(JSON.stringify({success:true,candidates:[]}));\n", encoding="utf-8")

    state_path = learner._site_dir(site_url) / "state.json"
    learner._write_json(
        state_path,
        {
            "site_url": learner.normalize_site_url(site_url),
            "skill_name": name,
            "generated_tool": {"enabled": True, "path": str(script)},
        },
    )

    context = learner.get_site_skill_context(site_url)

    assert context["has_skill"] is True
    assert context["has_tool"] is True
    assert context["tool_name"] == "dtc_site_search_tool"
    assert "example.com" in context["tool_intro"]


def test_generated_tool_index_is_site_scoped(tmp_path, monkeypatch):
    from agent import dtc_site_search_learner as learner

    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_SKILL_DIR", str(tmp_path / "skills"))

    enabled_site = "https://example.com"
    other_site = "https://other.example"
    enabled_script = learner._generated_tool_path(enabled_site)
    enabled_script.parent.mkdir(parents=True)
    enabled_script.write_text("console.log(JSON.stringify({success:true,candidates:[{}]}));\n", encoding="utf-8")

    learner._write_json(
        learner._site_dir(enabled_site) / "state.json",
        {
            "site_url": learner.normalize_site_url(enabled_site),
            "skill_name": learner.skill_name_for_site(enabled_site),
            "generated_tool": {
                "enabled": True,
                "path": str(enabled_script),
                "description": "Only for example.com",
            },
        },
    )
    learner.refresh_generated_tool_index()

    enabled_context = learner.get_site_skill_context(enabled_site)
    other_context = learner.get_site_skill_context(other_site)

    assert enabled_context["has_tool"] is True
    assert enabled_context["tool_intro"] == "Only for example.com"
    assert other_context["has_tool"] is False
    assert other_context["tool_intro"] == ""


def test_unchanged_skill_update_increments_stable_count_and_triggers_tool_generation(
    tmp_path,
    monkeypatch,
):
    from agent import dtc_site_search_learner as learner

    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_SKILL_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_TOOL_GENERATION_STABLE_UPDATES", "1")

    site_url = "https://example.com"
    normalized = learner.normalize_site_url(site_url)
    name = learner.skill_name_for_site(normalized)
    existing = "---\nname: test\n---\n\n## Minimal Successful Path\n\n- Existing route.\n"

    skill_md = learner._skill_dir(name) / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text(existing, encoding="utf-8")

    cleaned = learner._site_dir(normalized) / "cleaned" / "r1.md"
    cleaned.parent.mkdir(parents=True)
    cleaned.write_text("## Minimal successful chain\nExisting route.\n", encoding="utf-8")

    learner._write_json(
        learner._site_dir(normalized) / "state.json",
        {
            "site_url": normalized,
            "site_key": learner.site_key(normalized),
            "skill_name": name,
            "success_count": 2,
            "records": [
                {
                    "record_id": "r1",
                    "success": True,
                    "cleaned_path": str(cleaned),
                    "created_at": "now",
                }
            ],
        },
    )

    generated = []
    monkeypatch.setattr(learner, "_aux_llm", lambda *a, **k: existing)
    monkeypatch.setattr(learner, "_generate_site_tool", lambda url: generated.append(url))

    learner._promote_site_skill(normalized, success_count=2, window=1, mode="update")

    state = json.loads((learner._site_dir(normalized) / "state.json").read_text(encoding="utf-8"))
    assert state["last_skill_update_changed"] is False
    assert state["stable_skill_update_count"] == 1
    assert generated == [normalized]


def test_changed_skill_update_requires_ab_before_overwrite(tmp_path, monkeypatch):
    from agent import dtc_site_search_learner as learner

    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_SKILL_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_TOOL_GENERATION_STABLE_UPDATES", "3")

    site_url = "https://example.com"
    normalized = learner.normalize_site_url(site_url)
    name = learner.skill_name_for_site(normalized)
    existing = "---\nname: test\n---\n\n## Minimal Successful Path\n\n- Existing route.\n"
    candidate = "---\nname: test\n---\n\n## Minimal Successful Path\n\n- Candidate route.\n"

    skill_md = learner._skill_dir(name) / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text(existing, encoding="utf-8")

    cleaned = learner._site_dir(normalized) / "cleaned" / "r1.md"
    cleaned.parent.mkdir(parents=True)
    cleaned.write_text("## Minimal successful chain\nCandidate route.\n", encoding="utf-8")

    learner._write_json(
        learner._site_dir(normalized) / "state.json",
        {
            "site_url": normalized,
            "site_key": learner.site_key(normalized),
            "skill_name": name,
            "success_count": 3,
            "skill_review_prior_stable_count": 1,
            "records": [
                {
                    "record_id": "r1",
                    "success": True,
                    "cleaned_path": str(cleaned),
                    "created_at": "now",
                }
            ],
        },
    )

    monkeypatch.setattr(learner, "_aux_llm", lambda *a, **k: candidate)
    monkeypatch.setattr(
        learner,
        "_evaluate_skill_update_candidate",
        lambda *a, **k: {"pass": False, "reason": "no token reduction"},
    )

    learner._promote_site_skill(normalized, success_count=3, window=1, mode="update")

    state = json.loads((learner._site_dir(normalized) / "state.json").read_text(encoding="utf-8"))
    assert skill_md.read_text(encoding="utf-8") == existing
    assert state["last_skill_update_changed"] is False
    assert state["last_skill_update_candidate_evaluation"]["pass"] is False
    assert state["stable_skill_update_count"] == 2
    assert state["y_no_change_count"] == 2


def test_context_lookup_does_not_run_lifecycle_promotion(tmp_path, monkeypatch):
    from agent import dtc_site_search_learner as learner

    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_SKILL_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_UPDATE_WINDOW", "1")

    normalized = learner.normalize_site_url("https://example.com")
    name = learner.skill_name_for_site(normalized)
    skill_md = learner._skill_dir(name) / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("---\nname: test\n---\n\n## Minimal Successful Path\n- Existing.\n", encoding="utf-8")
    learner._write_json(
        learner._site_dir(normalized) / "state.json",
        {
            "site_url": normalized,
            "site_key": learner.site_key(normalized),
            "skill_name": name,
            "success_count": 10,
            "last_skill_update_success_count": 1,
            "skill_status": "active",
        },
    )
    monkeypatch.setattr(
        learner,
        "_promote_site_skill",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("context must not promote")),
    )

    context = learner.get_site_skill_context(normalized)

    assert context["has_skill"] is True
    state = learner._read_json(learner._site_dir(normalized) / "state.json", {})
    assert state["skill_status"] == "active"


def test_initial_skill_candidate_rejected_resets_n_window_without_writing_skill(tmp_path, monkeypatch):
    from agent import dtc_site_search_learner as learner

    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_SKILL_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_INDEX_SKILL_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_SUCCESS_THRESHOLD", "2")

    site_url = "https://example.com"
    normalized = learner.normalize_site_url(site_url)
    name = learner.skill_name_for_site(normalized)
    root = learner._site_dir(normalized)
    cleaned1 = root / "cleaned" / "r1.md"
    cleaned2 = root / "cleaned" / "r2.md"
    cleaned1.parent.mkdir(parents=True)
    cleaned1.write_text("Search broadly and eventually find /products/a.", encoding="utf-8")
    cleaned2.write_text("Search broadly and eventually find /products/b.", encoding="utf-8")
    learner._write_json(
        root / "state.json",
        {
            "site_url": normalized,
            "skill_name": name,
            "success_count": 2,
            "records": [
                {"record_id": "r1", "success": True, "cleaned_path": str(cleaned1)},
                {"record_id": "r2", "success": True, "cleaned_path": str(cleaned2)},
            ],
        },
    )

    monkeypatch.setattr(learner, "_aux_llm", lambda *a, **k: "---\nname: bad\n---\n\nNo reusable route.", raising=False)

    learner._promote_site_skill(normalized, success_count=2, window=2, mode="create")

    state = learner._read_json(root / "state.json", {})
    assert not (learner._skill_dir(name) / "SKILL.md").exists()
    assert state["skill_status"] == "none"
    assert state["last_skill_update_success_count"] == 2
    assert state["n_success_count"] == 0
    assert state["last_skill_candidate_evaluation"]["pass"] is False


def test_initial_skill_candidate_passes_ab_and_publishes_skill(tmp_path, monkeypatch):
    from agent import dtc_site_search_learner as learner

    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_SKILL_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_INDEX_SKILL_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_SUCCESS_THRESHOLD", "2")

    site_url = "https://example.com"
    normalized = learner.normalize_site_url(site_url)
    name = learner.skill_name_for_site(normalized)
    root = learner._site_dir(normalized)
    cleaned1 = root / "cleaned" / "r1.md"
    cleaned2 = root / "cleaned" / "r2.md"
    cleaned1.parent.mkdir(parents=True)
    cleaned_text = (
        "Minimal successful chain: open direct search, inspect /products/ links. "
        "Wasted path: do not browse categories first. " * 80
    )
    cleaned1.write_text(cleaned_text, encoding="utf-8")
    cleaned2.write_text(cleaned_text, encoding="utf-8")
    learner._write_json(
        root / "state.json",
        {
            "site_url": normalized,
            "skill_name": name,
            "success_count": 2,
            "records": [
                {"record_id": "r1", "success": True, "cleaned_path": str(cleaned1)},
                {"record_id": "r2", "success": True, "cleaned_path": str(cleaned2)},
            ],
        },
    )
    good_skill = f"""---
name: {name}
---

## When To Use
Use for example.com.

## Minimal Successful Path
- Open direct search and inspect `/products/` links.

## Do Not Do
- Do not browse categories before direct search.

## Product Discovery Shortcuts
- Prefer `/products/` URLs.

## Verification Hints
- Verify product title and URL.
"""
    monkeypatch.setattr(learner, "_aux_llm", lambda *a, **k: good_skill, raising=False)

    learner._promote_site_skill(normalized, success_count=2, window=2, mode="create")

    state = learner._read_json(root / "state.json", {})
    assert (learner._skill_dir(name) / "SKILL.md").exists()
    assert state["skill_status"] == "active"
    assert state["last_skill_update_success_count"] == 2
    assert state["n_success_count"] == 0
    assert state["x_success_count"] == 0
    assert state["last_skill_candidate_evaluation"]["pass"] is True
    assert state["last_skill_candidate_published_at"]


def test_initial_skill_creation_ab_uses_actual_rerun_cases(tmp_path, monkeypatch):
    from agent import dtc_site_search_learner as learner

    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", str(tmp_path / "data"))

    site_url = "https://example.com"
    normalized = learner.normalize_site_url(site_url)
    root = learner._site_dir(normalized)
    cleaned = root / "cleaned" / "r1.md"
    cleaned.parent.mkdir(parents=True)
    cleaned.write_text("Use direct search, then open /products/a.", encoding="utf-8")
    learner._write_json(
        root / "raw" / "r1.json",
        {
            "site_url": normalized,
            "sku_id": "sku1",
            "payload": {
                "site_url": normalized,
                "sku_id": "sku1",
                "product_urls": ["https://example.com/products/a"],
                "candidate_products": [{"title": "A", "url": "https://example.com/products/a"}],
                "total_tokens": 200,
                "tool_call_count": 4,
                "elapsed_seconds": 10.0,
            },
        },
    )
    learner._write_json(
        root / "state.json",
        {
            "site_url": normalized,
            "success_count": 1,
            "records": [{"record_id": "r1", "success": True, "cleaned_path": str(cleaned)}],
        },
    )
    calls = []

    def fake_run(case, candidate_skill, max_iterations=30):
        calls.append((case, candidate_skill))
        return {
            "record_id": case["record_id"],
            "baseline": {"total_tokens": 200},
            "candidate": {"total_tokens": 100},
            "same_or_better_output": True,
            "token_reduction_rate": 0.5,
        }

    monkeypatch.setattr(learner, "_run_initial_skill_ab_case", fake_run)

    result = learner._evaluate_initial_skill_candidate(
        normalized,
        "---\nname: good\n---\n\n## Minimal Successful Path\n- Search and open `/products/`.\n\n## Do Not Do\n- Do not browse categories.\n\n## Product Discovery Shortcuts\n- `/products/`.\n\n## Verification Hints\n- Verify title.\n",
        [cleaned.read_text(encoding="utf-8")],
    )

    assert result["pass"] is True
    assert result["ab_mode"] == "recorded_baseline_vs_candidate_skill_rerun"
    assert result["cases"] == 1
    assert result["baseline_avg_total_tokens"] == 200
    assert result["candidate_avg_total_tokens"] == 100
    assert calls[0][0]["expected"] == "https://example.com/products/a"
    assert calls[0][0]["baseline"]["total_tokens"] == 200


def test_initial_skill_ab_subtracts_candidate_skill_prompt_tokens(monkeypatch):
    from agent import dtc_site_search_ab as ab
    from agent import dtc_site_search_learner as learner

    monkeypatch.setattr(
        ab,
        "run_agent_prompt",
        lambda *a, **k: {
            "final_response": "https://example.com/products/a",
            "total_tokens": 250,
            "events": [],
        },
    )
    monkeypatch.setattr(ab, "score_response", lambda response, expected: {"pass": response == expected})

    candidate_skill = "x" * 400
    result = learner._run_initial_skill_ab_case(
        {
            "record_id": "r1",
            "sku_id": "sku1",
            "expected": "https://example.com/products/a",
            "baseline": {"total_tokens": 200},
            "prompt": "find sku1",
        },
        candidate_skill,
    )

    assert result["raw_candidate_total_tokens"] == 250
    assert result["candidate_skill_token_adjustment"] == 100
    assert result["adjusted_candidate_total_tokens"] == 150
    assert result["token_delta"] == -50
    assert result["token_reduction_rate"] == 0.25


def test_initial_skill_creation_ab_rejects_when_actual_rerun_has_no_efficiency_gain(tmp_path, monkeypatch):
    from agent import dtc_site_search_learner as learner

    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", str(tmp_path / "data"))

    site_url = "https://example.com"
    normalized = learner.normalize_site_url(site_url)
    root = learner._site_dir(normalized)
    cleaned = root / "cleaned" / "r1.md"
    cleaned.parent.mkdir(parents=True)
    cleaned.write_text("Use direct search, then open /products/a.", encoding="utf-8")
    learner._write_json(
        root / "raw" / "r1.json",
        {
            "site_url": normalized,
            "sku_id": "sku1",
            "payload": {
                "site_url": normalized,
                "sku_id": "sku1",
                "product_urls": ["https://example.com/products/a"],
                "total_tokens": 200,
                "tool_call_count": 4,
                "elapsed_seconds": 10.0,
            },
        },
    )
    learner._write_json(
        root / "state.json",
        {
            "site_url": normalized,
            "success_count": 1,
            "records": [{"record_id": "r1", "success": True, "cleaned_path": str(cleaned)}],
        },
    )
    monkeypatch.setattr(
        learner,
        "_run_initial_skill_ab_case",
        lambda *a, **k: {
            "record_id": "r1",
            "baseline": {"total_tokens": 200},
            "candidate": {"total_tokens": 200},
            "same_or_better_output": True,
            "token_reduction_rate": 0.0,
        },
    )

    result = learner._evaluate_initial_skill_candidate(
        normalized,
        "---\nname: good\n---\n\n## Minimal Successful Path\n- Search and open `/products/`.\n\n## Do Not Do\n- Do not browse categories.\n\n## Product Discovery Shortcuts\n- `/products/`.\n\n## Verification Hints\n- Verify title.\n",
        [cleaned.read_text(encoding="utf-8")],
    )

    assert result["pass"] is False
    assert result["passed_output"] == 1
    assert result["average_token_delta"] == 0


def test_initial_skill_creation_ab_uses_batch_average_tokens(tmp_path, monkeypatch):
    from agent import dtc_site_search_learner as learner

    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", str(tmp_path / "data"))

    site_url = "https://example.com"
    normalized = learner.normalize_site_url(site_url)
    root = learner._site_dir(normalized)
    records = []
    cleaned_chunks = []
    for idx in range(3):
        record_id = f"r{idx + 1}"
        cleaned = root / "cleaned" / f"{record_id}.md"
        cleaned.parent.mkdir(parents=True, exist_ok=True)
        cleaned.write_text(f"Use direct search, then open /products/{idx}.", encoding="utf-8")
        learner._write_json(
            root / "raw" / f"{record_id}.json",
            {
                "site_url": normalized,
                "sku_id": f"sku{idx + 1}",
                "payload": {
                    "site_url": normalized,
                    "sku_id": f"sku{idx + 1}",
                    "product_urls": [f"https://example.com/products/{idx}"],
                    "total_tokens": 100,
                },
            },
        )
        records.append({"record_id": record_id, "success": True, "cleaned_path": str(cleaned)})
        cleaned_chunks.append(cleaned.read_text(encoding="utf-8"))
    learner._write_json(
        root / "state.json",
        {"site_url": normalized, "success_count": 3, "records": records},
    )
    candidate_tokens = {"r1": 120, "r2": 80, "r3": 70}

    def fake_run(case, candidate_skill, max_iterations=30):
        return {
            "record_id": case["record_id"],
            "baseline": {"total_tokens": 100},
            "candidate": {"total_tokens": candidate_tokens[case["record_id"]]},
            "same_or_better_output": True,
        }

    monkeypatch.setattr(learner, "_run_initial_skill_ab_case", fake_run)

    result = learner._evaluate_initial_skill_candidate(
        normalized,
        "---\nname: good\n---\n\n## Minimal Successful Path\n- Search and open `/products/`.\n\n## Do Not Do\n- Do not browse categories.\n\n## Product Discovery Shortcuts\n- `/products/`.\n\n## Verification Hints\n- Verify title.\n",
        cleaned_chunks,
    )

    assert result["pass"] is True
    assert result["cases"] == 3
    assert result["baseline_avg_total_tokens"] == 100
    assert result["candidate_avg_total_tokens"] == 90
    assert result["average_token_delta"] == -10


def test_initial_skill_creation_claim_freezes_n_during_testing(tmp_path, monkeypatch):
    from agent import dtc_site_search_learner as learner

    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_SKILL_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_SUCCESS_THRESHOLD", "2")
    monkeypatch.setattr(learner, "_aux_llm", lambda *a, **k: "## Result\nCleaned\n", raising=False)
    calls = []

    def fake_promote(site_url, success_count, window, mode="update"):
        calls.append((site_url, success_count, window, mode))

    monkeypatch.setattr(learner, "_promote_site_skill", fake_promote)

    normalized = learner.normalize_site_url("https://example.com")
    for idx in range(3):
        raw = {
            "record_id": f"r{idx + 1}",
            "created_at": learner._utc_now(),
            "site_url": normalized,
            "site_key": learner.site_key(normalized),
            "sku_id": f"sku{idx + 1}",
            "success": True,
            "payload": {
                "site_url": normalized,
                "sku_id": f"sku{idx + 1}",
                "success": True,
                "route_used": "skill_only",
                "exploration_summary": "Found product.",
                "product_urls": [f"https://example.com/products/{idx + 1}"],
            },
        }
        learner._clean_and_promote(normalized, raw["record_id"], raw)

    state = learner._read_json(learner._site_dir(normalized) / "state.json", {})
    assert calls == [(normalized, 2, 2, "create")]
    assert state["success_count"] == 3
    assert state["skill_status"] == "testing"
    assert state["n_success_count"] == 0


def test_generated_tool_historical_ab_evaluation(tmp_path, monkeypatch):
    from agent import dtc_site_search_learner as learner

    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", str(tmp_path / "data"))

    site_url = "https://example.com"
    root = learner._site_dir(site_url)
    record_id = "r1"
    learner._write_json(
        root / "raw" / f"{record_id}.json",
        {
            "payload": {
                "exploration_summary": "Found Acme Widget on example.com.",
                "product_urls": ["https://www.example.com/products/acme-widget"],
                "candidate_products": [
                    {
                        "title": "Acme Widget",
                        "url": "https://www.example.com/products/acme-widget",
                    }
                ],
            }
        },
    )

    def fake_run(script_path, payload, timeout=60):
        return {
            "success": True,
            "output": {
                "success": True,
                "candidates": [
                    {"url": "https://example.com/products/acme-widget"}
                ],
            },
        }

    monkeypatch.setattr(learner, "_run_generated_tool_script", fake_run)
    result = learner._evaluate_generated_tool_candidate(
        tmp_path / "tool.mjs",
        site_url,
        root,
        [{"record_id": record_id}],
    )

    assert result["pass"] is True
    assert result["cases"] == 1
    assert result["passed"] == 1


def test_generated_tool_historical_ab_requires_judged_case(tmp_path, monkeypatch):
    from agent import dtc_site_search_learner as learner

    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", str(tmp_path / "data"))

    site_url = "https://example.com"
    root = learner._site_dir(site_url)
    learner._write_json(
        root / "raw" / "r1.json",
        {"payload": {"exploration_summary": "No expected URL was recorded."}},
    )

    result = learner._evaluate_generated_tool_candidate(
        tmp_path / "tool.mjs",
        site_url,
        root,
        [{"record_id": "r1"}],
    )

    assert result["pass"] is False
    assert result["cases"] == 0
    assert result["passed"] == 0


def _activate_generated_tool(learner, site_url, script, version="tool-v1"):
    learner._write_json(
        learner._site_dir(site_url) / "state.json",
        {
            "site_url": learner.normalize_site_url(site_url),
            "site_key": learner.site_key(site_url),
            "skill_name": learner.skill_name_for_site(site_url),
            "success_count": 2,
            "last_skill_update_success_count": 2,
            "stable_skill_update_count": 2,
            "generated_tool": {
                "enabled": True,
                "status": "enabled",
                "path": str(script),
                "version": version,
            },
            "tool_status": "active",
            "active_tool_version": version,
            "active_route": "tool_first",
            "z_tool_fail_count": 0,
        },
    )


def test_tool_success_does_not_increment_z_or_skill_review_counts(tmp_path, monkeypatch):
    from agent import dtc_site_search_learner as learner

    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_SKILL_DIR", str(tmp_path / "skills"))

    site_url = "https://example.com"
    script = learner._generated_tool_versions_dir(site_url) / "tool-v1" / "site_search_tool.mjs"
    script.parent.mkdir(parents=True)
    script.write_text(
        "process.stdout.write(JSON.stringify({success:true,candidates:[{url:'https://example.com/products/a'}]}));\n",
        encoding="utf-8",
    )
    _activate_generated_tool(learner, site_url, script)

    result = learner.run_generated_site_tool(site_url, "Product A", tool_call_id="tc-success")
    state = learner._read_json(learner._site_dir(site_url) / "state.json", {})

    assert result["success"] is True
    assert state["z_tool_fail_count"] == 0
    assert state["stable_skill_update_count"] == 2
    assert state["active_route"] == "tool_first"


def test_tool_success_record_does_not_advance_x_y(tmp_path, monkeypatch):
    from agent import dtc_site_search_learner as learner

    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_SKILL_DIR", str(tmp_path / "skills"))
    monkeypatch.setattr(learner, "_aux_llm", lambda *a, **k: "## Result\nSuccess\n", raising=False)

    site_url = "https://example.com"
    normalized = learner.normalize_site_url(site_url)
    state_path = learner._site_dir(normalized) / "state.json"
    learner._write_json(
        state_path,
        {
            "site_url": normalized,
            "skill_name": learner.skill_name_for_site(normalized),
            "success_count": 2,
            "last_skill_update_success_count": 2,
            "stable_skill_update_count": 2,
        },
    )
    raw = {
        "record_id": "tool-success-record",
        "created_at": learner._utc_now(),
        "site_url": normalized,
        "site_key": learner.site_key(normalized),
        "sku_id": "sku1",
        "success": True,
        "payload": {
            "site_url": normalized,
            "sku_id": "sku1",
            "success": True,
            "tool_success": True,
            "route_used": "tool_first",
            "tool_version": "tool-v1",
            "exploration_summary": "Tool returned candidates.",
        },
    }

    learner._clean_and_promote(normalized, raw["record_id"], raw)
    state = learner._read_json(state_path, {})

    assert state["success_count"] == 2
    assert state["tool_success_count"] == 1
    assert state["stable_skill_update_count"] == 2


def test_tool_failure_counts_z_and_keeps_tool_first_before_threshold(tmp_path, monkeypatch):
    from agent import dtc_site_search_learner as learner

    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_TOOL_FAILURE_DISABLE_THRESHOLD", "3")

    site_url = "https://example.com"
    script = learner._generated_tool_versions_dir(site_url) / "tool-v1" / "site_search_tool.mjs"
    script.parent.mkdir(parents=True)
    script.write_text("process.stdout.write(JSON.stringify({success:false,error:'empty result'}));\n", encoding="utf-8")
    _activate_generated_tool(learner, site_url, script)

    result = learner.run_generated_site_tool(site_url, "Product A", tool_call_id="tc-fail-1")
    state = learner._read_json(learner._site_dir(site_url) / "state.json", {})

    assert result["success"] is False
    assert result["failure"]["counted"] is True
    assert state["z_tool_fail_count"] == 1
    assert state["tool_status"] == "active"
    assert state["active_route"] == "tool_first"
    assert state["tool_failure_history"][-1]["failure_type"] == "empty_result"


def test_tool_disabled_at_z_threshold_and_returns_to_skill_only(tmp_path, monkeypatch):
    from agent import dtc_site_search_learner as learner

    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_TOOL_FAILURE_DISABLE_THRESHOLD", "3")

    site_url = "https://example.com"
    script = learner._generated_tool_versions_dir(site_url) / "tool-v1" / "site_search_tool.mjs"
    script.parent.mkdir(parents=True)
    script.write_text("process.exit(2);\n", encoding="utf-8")
    _activate_generated_tool(learner, site_url, script)

    for idx in range(3):
        learner.run_generated_site_tool(site_url, "Product A", tool_call_id=f"tc-fail-{idx}")
    state = learner._read_json(learner._site_dir(site_url) / "state.json", {})
    result_after_disable = learner.run_generated_site_tool(site_url, "Product A", tool_call_id="tc-after-disabled")

    assert state["tool_status"] == "disabled"
    assert state["active_route"] == "skill_only"
    assert state["z_tool_fail_count"] == 0
    assert state["success_count"] == 2
    assert state["stable_skill_update_count"] == 0
    assert state["x_success_count"] == 0
    assert state["y_no_change_count"] == 0
    assert result_after_disable["has_tool"] is False


def test_new_tool_version_starts_with_zero_z_count(tmp_path, monkeypatch):
    from agent import dtc_site_search_learner as learner

    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_TOOL_FAILURE_DISABLE_THRESHOLD", "1")

    site_url = "https://example.com"
    old_script = learner._generated_tool_versions_dir(site_url) / "tool-v1" / "site_search_tool.mjs"
    old_script.parent.mkdir(parents=True)
    old_script.write_text("process.exit(2);\n", encoding="utf-8")
    _activate_generated_tool(learner, site_url, old_script, version="tool-v1")
    learner.run_generated_site_tool(site_url, "Product A", tool_call_id="tc-old-fail")

    new_script = learner._generated_tool_versions_dir(site_url) / "tool-v2" / "site_search_tool.mjs"
    new_script.parent.mkdir(parents=True)
    new_script.write_text(
        "process.stdout.write(JSON.stringify({success:true,candidates:[{url:'https://example.com/products/a'}]}));\n",
        encoding="utf-8",
    )
    _activate_generated_tool(learner, site_url, new_script, version="tool-v2")
    state = learner._read_json(learner._site_dir(site_url) / "state.json", {})
    result = learner.run_generated_site_tool(site_url, "Product A", tool_call_id="tc-new-success")

    assert state["active_tool_version"] == "tool-v2"
    assert state["z_tool_fail_count"] == 0
    assert result["success"] is True
    assert result["tool_version"] == "tool-v2"


def test_duplicate_and_concurrent_tool_failure_events_count_once(tmp_path, monkeypatch):
    from agent import dtc_site_search_learner as learner

    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_TOOL_FAILURE_DISABLE_THRESHOLD", "2")

    site_url = "https://example.com"
    script = learner._generated_tool_versions_dir(site_url) / "tool-v1" / "site_search_tool.mjs"
    script.parent.mkdir(parents=True)
    script.write_text("process.exit(2);\n", encoding="utf-8")
    _activate_generated_tool(learner, site_url, script)

    def record_same_event():
        learner.record_generated_tool_failure(
            site_url,
            "Product A",
            {"success": False, "error": "node exited 2", "tool_version": "tool-v1"},
            tool_call_id="same-tool-call",
        )

    threads = [threading.Thread(target=record_same_event) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    state = learner._read_json(learner._site_dir(site_url) / "state.json", {})

    assert state["z_tool_fail_count"] == 1
    assert state["tool_status"] == "active"
    assert len([e for e in state["counted_tool_failure_events"] if e == "tool_call:same-tool-call"]) == 1


def test_tool_failure_with_skill_fallback_success_records_both_paths(tmp_path, monkeypatch):
    from agent import dtc_site_search_learner as learner

    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_SKILL_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_TOOL_FAILURE_DISABLE_THRESHOLD", "3")
    monkeypatch.setattr(learner, "_aux_llm", lambda *a, **k: "## Result\nFallback success\n", raising=False)

    site_url = "https://example.com"
    normalized = learner.normalize_site_url(site_url)
    script = learner._generated_tool_versions_dir(normalized) / "tool-v1" / "site_search_tool.mjs"
    script.parent.mkdir(parents=True)
    script.write_text("process.exit(2);\n", encoding="utf-8")
    _activate_generated_tool(learner, normalized, script)

    failed_tool = learner.run_generated_site_tool(normalized, "Product A", tool_call_id="tc-fallback-success")
    raw = {
        "record_id": "fallback-success",
        "created_at": learner._utc_now(),
        "site_url": normalized,
        "site_key": learner.site_key(normalized),
        "sku_id": "sku1",
        "success": True,
        "payload": {
            "site_url": normalized,
            "sku_id": "sku1",
            "success": True,
            "route_used": "skill_only",
            "tool_failure_event_id": failed_tool["failure"]["failure_event_id"],
            "exploration_summary": "Tool failed, skill fallback found product.",
            "product_urls": ["https://example.com/products/a"],
            "candidate_products": [{"title": "Product A", "url": "https://example.com/products/a"}],
        },
    }
    learner._clean_and_promote(normalized, raw["record_id"], raw)
    state = learner._read_json(learner._site_dir(normalized) / "state.json", {})

    assert failed_tool["success"] is False
    assert state["z_tool_fail_count"] == 1
    assert state["success_count"] == 3
    assert state["records"][-1]["success"] is True
    assert state["tool_failure_history"][-1]["tool_call_id"] == "tc-fallback-success"


def test_tool_failure_with_skill_fallback_failure_still_counts_z(tmp_path, monkeypatch):
    from agent import dtc_site_search_learner as learner

    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_SKILL_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("HERMES_DTC_SITE_SEARCH_TOOL_FAILURE_DISABLE_THRESHOLD", "3")
    monkeypatch.setattr(learner, "_aux_llm", lambda *a, **k: "## Result\nFallback failed\n", raising=False)

    site_url = "https://example.com"
    normalized = learner.normalize_site_url(site_url)
    script = learner._generated_tool_versions_dir(normalized) / "tool-v1" / "site_search_tool.mjs"
    script.parent.mkdir(parents=True)
    script.write_text("process.exit(2);\n", encoding="utf-8")
    _activate_generated_tool(learner, normalized, script)

    failed_tool = learner.run_generated_site_tool(normalized, "Product A", tool_call_id="tc-fallback-fail")
    raw = {
        "record_id": "fallback-fail",
        "created_at": learner._utc_now(),
        "site_url": normalized,
        "site_key": learner.site_key(normalized),
        "sku_id": "sku1",
        "success": False,
        "payload": {
            "site_url": normalized,
            "sku_id": "sku1",
            "success": False,
            "route_used": "skill_only",
            "tool_failure_event_id": failed_tool["failure"]["failure_event_id"],
            "exploration_summary": "Tool failed and skill fallback failed.",
        },
    }
    learner._clean_and_promote(normalized, raw["record_id"], raw)
    state = learner._read_json(learner._site_dir(normalized) / "state.json", {})

    assert failed_tool["success"] is False
    assert state["z_tool_fail_count"] == 1
    assert state["success_count"] == 2
    assert state["records"][-1]["success"] is False
