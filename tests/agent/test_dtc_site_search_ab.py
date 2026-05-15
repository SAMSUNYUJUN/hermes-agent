from agent.dtc_site_search_ab import DtcSiteSearchAbCase, run_ab_cases, score_response


class FakeAgent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def run_conversation(self, prompt, conversation_history=None):
        tool_start = self.kwargs["tool_start_callback"]
        tool_complete = self.kwargs["tool_complete_callback"]
        if "A/B mode: candidate" in prompt:
            tool_start("1", "dtc_site_search_tool", {"site_url": "https://hsn.com"})
            tool_complete("1", "dtc_site_search_tool", {"site_url": "https://hsn.com"}, "{}")
            return {
                "final_response": "https://www.hsn.com/products/birkenstock-madrid-sandal/23521196",
                "api_calls": 1,
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "estimated_cost_usd": 0.001,
            }
        tool_start("1", "skill_view", {"name": "dtc-site-hsn-com"})
        tool_complete("1", "skill_view", {"name": "dtc-site-hsn-com"}, "{}")
        return {
            "final_response": "https://www.hsn.com/products/birkenstock-madrid-sandal/23521196",
            "api_calls": 1,
            "input_tokens": 300,
            "output_tokens": 60,
            "total_tokens": 360,
            "estimated_cost_usd": 0.003,
        }


def test_score_response_accepts_expected_url():
    score = score_response(
        "Found https://www.hsn.com/products/birkenstock-madrid-sandal/23521196",
        "https://hsn.com/products/birkenstock-madrid-sandal/23521196",
    )

    assert score["pass"] is True


def test_ab_runner_marks_candidate_publish_ready_with_token_reduction():
    case = DtcSiteSearchAbCase(
        index=4,
        sku_id="1730980628884132124",
        domain="hsn.com",
        prompt='check if sku_id 1730980628884132124 has a similar or same products in "hsn.com"',
        expected="https://www.hsn.com/products/birkenstock-madrid-sandal/23521196",
    )

    report = run_ab_cases(
        [case],
        modes=["candidate", "baseline_skill"],
        concurrency=2,
        max_iterations=30,
        agent_factory=FakeAgent,
        min_token_reduction_rate=0.05,
    )

    summary = report["summary"]
    assert summary["paired_count"] == 1
    assert summary["paired_pass_count"] == 1
    assert summary["publish_ready"] is True
    assert summary["paired"][0]["token_reduction_rate"] > 0.6
    candidate = [row for row in report["results"] if row["mode"] == "candidate"][0]
    baseline = [row for row in report["results"] if row["mode"] == "baseline_skill"][0]
    assert candidate["used_generated_tool"] is True
    assert baseline["used_skill"] is True
