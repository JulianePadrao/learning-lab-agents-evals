"""
Compare agent responses to ground truth using an LLM-as-judge.

Reads a results JSONL (query + response + ground_truth), scores each item
on accuracy, completeness, and tone, and writes a markdown report.

Environment variables:
    FOUNDRY_PROJECT_ENDPOINT – Azure AI Foundry project endpoint
    RESULTS_FILE             – path to the results JSONL to score
    MODEL_NAME               – (optional) deployed model name, default: gpt-4.1

Authentication uses DefaultAzureCredential (managed identity in CI,
az login / VS Code locally).
"""

import json
import os
import re
import sys
from pathlib import Path
from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
RESULTS_FILE = os.environ.get("RESULTS_FILE")
MODEL_NAME = os.environ.get("MODEL_NAME", "gpt-4.1")

if not ENDPOINT:
    print("ERROR: FOUNDRY_PROJECT_ENDPOINT is not set.")
    sys.exit(1)

if not RESULTS_FILE:
    print("ERROR: RESULTS_FILE is not set.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

JUDGE_SYSTEM_PROMPT = """\
You are a technical accuracy judge for Microsoft Learn training content.

Score the RESPONSE against the GROUND TRUTH on three criteria (1-5 scale):

**Accuracy** — Are product names, service names, and technical facts correct?
- 1: Wrong product names or incorrect facts
- 3: Correct core facts, minor omissions
- 5: Fully correct terminology and facts

**Completeness** — Does the response cover the key points from the ground truth?
- 1: Misses key points
- 3: Covers main points
- 5: Covers all points from ground truth

**Tone** — Is the response learner-focused and appropriate for training content?
- 1: Robotic or off-brand
- 3: Acceptable but generic
- 5: Learner-focused, matches training style

Reply with ONLY a JSON object (no markdown fences):
{"accuracy": <int>, "completeness": <int>, "tone": <int>, "notes": "<brief explanation>"}
"""


def load_results(path: str) -> list[dict]:
    """Load JSONL results file."""
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def extract_agent_name(results_path: str) -> str:
    """Derive agent name from results filename like smoke-test-results-learn-module-writer.jsonl."""
    stem = Path(results_path).stem  # smoke-test-results-learn-module-writer
    prefix = "smoke-test-results-"
    if stem.startswith(prefix):
        return stem[len(prefix):]
    return stem


def parse_judge_response(text: str) -> dict:
    """Parse the judge LLM's JSON response, handling markdown fences if present."""
    # Strip markdown code fences if the model wraps its response
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {"accuracy": 1, "completeness": 1, "tone": 1, "notes": f"Failed to parse judge response: {text[:200]}"}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    print(f"Results file: {RESULTS_FILE}")
    print(f"Judge model:  {MODEL_NAME}")

    results = load_results(RESULTS_FILE)
    print(f"Items to score: {len(results)}")

    agent_name = extract_agent_name(RESULTS_FILE)
    print(f"Agent name: {agent_name}")

    project_client = AIProjectClient(
        endpoint=ENDPOINT,
        credential=DefaultAzureCredential(),
    )
    client = project_client.get_openai_client()

    scores = []
    for i, item in enumerate(results, 1):
        query = item["query"]
        response = item["response"]
        ground_truth = item["ground_truth"]

        print(f"  [{i}/{len(results)}] Scoring: {query[:70]}...")

        user_prompt = (
            f"QUERY: {query}\n\n"
            f"GROUND TRUTH: {ground_truth}\n\n"
            f"RESPONSE: {response}"
        )

        judge_response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            max_tokens=256,
        )

        judge_text = judge_response.choices[0].message.content.strip()
        score = parse_judge_response(judge_text)
        score["query"] = query
        score["response"] = response
        score["ground_truth"] = ground_truth
        score["verdict"] = "PASS" if score.get("accuracy", 0) >= 3 and score.get("completeness", 0) >= 3 else "FAIL"
        scores.append(score)

    # Aggregate metrics
    total = len(scores)
    pass_count = sum(1 for s in scores if s["verdict"] == "PASS")
    pass_rate = (pass_count / total * 100) if total else 0

    acc_scores = [s["accuracy"] for s in scores]
    comp_scores = [s["completeness"] for s in scores]
    tone_scores = [s["tone"] for s in scores]

    def avg(lst):
        return sum(lst) / len(lst) if lst else 0

    # Write report
    report_dir = Path("reports")
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{agent_name}-smoke-test-report.md"

    lines = [
        f"# Smoke Test Report: {agent_name}",
        "",
        f"**Date:** {os.popen('date +%Y-%m-%d 2>/dev/null || echo unknown').read().strip()}",
        f"**Test file:** smoke-test.jsonl",
        f"**Total questions:** {total}",
        f"**Pass rate:** {pass_count}/{total} ({pass_rate:.0f}%)",
        "",
        "## Summary",
        "",
        "| Criterion | Average | Min | Max |",
        "|-----------|---------|-----|-----|",
        f"| Accuracy | {avg(acc_scores):.1f} | {min(acc_scores)} | {max(acc_scores)} |",
        f"| Completeness | {avg(comp_scores):.1f} | {min(comp_scores)} | {max(comp_scores)} |",
        f"| Tone | {avg(tone_scores):.1f} | {min(tone_scores)} | {max(tone_scores)} |",
        "",
        "## Details",
        "",
    ]

    for i, s in enumerate(scores, 1):
        lines.extend([
            f"### Q{i}: {s['query']}",
            "",
            f"- **Ground truth:** {s['ground_truth']}",
            f"- **Response:** {s['response']}",
            f"- **Scores:** Accuracy: {s['accuracy']} | Completeness: {s['completeness']} | Tone: {s['tone']}",
            f"- **Verdict:** {s['verdict']}",
            f"- **Notes:** {s.get('notes', '')}",
            "",
        ])

    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport written to {report_path}")

    # Print summary to stdout for CI
    print(f"\n{'=' * 60}")
    print(f"  {agent_name} — Pass rate: {pass_count}/{total} ({pass_rate:.0f}%)")
    print(f"  Accuracy: {avg(acc_scores):.1f}  Completeness: {avg(comp_scores):.1f}  Tone: {avg(tone_scores):.1f}")
    print(f"{'=' * 60}")

    # Exit with failure if pass rate < 80%
    if pass_rate < 80:
        print(f"\nFAILED: Pass rate {pass_rate:.0f}% is below 80% threshold.")
        sys.exit(1)


if __name__ == "__main__":
    main()
