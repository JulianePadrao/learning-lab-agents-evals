"""
Generate agent responses for a smoke-test evaluation.

Reads an agent .agent.md file, extracts its system prompt, sends each query
through an Azure AI Foundry model deployment, and writes a results JSONL with
query + response + ground_truth.

Environment variables:
    FOUNDRY_PROJECT_ENDPOINT – Azure AI Foundry project endpoint
    AGENT_FILE               – path to the agent .agent.md file
    TEST_FILE                – path to the test JSONL file
    RESULTS_FILE             – path to write the results JSONL
    MODEL_NAME               – (optional) deployed model name, default: gpt-4.1

Authentication uses DefaultAzureCredential (managed identity in CI,
az login / VS Code locally).
"""

import json
import os
import sys
from pathlib import Path
from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
AGENT_FILE = os.environ.get("AGENT_FILE")
TEST_FILE = os.environ.get("TEST_FILE")
RESULTS_FILE = os.environ.get("RESULTS_FILE")
MODEL_NAME = os.environ.get("MODEL_NAME", "gpt-4.1")

if not ENDPOINT:
    print("ERROR: FOUNDRY_PROJECT_ENDPOINT is not set.")
    sys.exit(1)

for var_name, var_val in [("AGENT_FILE", AGENT_FILE), ("TEST_FILE", TEST_FILE), ("RESULTS_FILE", RESULTS_FILE)]:
    if not var_val:
        print(f"ERROR: {var_name} is not set.")
        sys.exit(1)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def extract_system_prompt(agent_path: str) -> str:
    """Read an .agent.md file and return everything after the YAML frontmatter."""
    text = Path(agent_path).read_text(encoding="utf-8")

    # Frontmatter is between the first two '---' lines
    if text.startswith("---"):
        end_index = text.index("---", 3)
        return text[end_index + 3:].strip()

    # No frontmatter — use the entire file
    return text.strip()


def load_test_items(test_path: str) -> list[dict]:
    """Load JSONL test file into a list of dicts with query and ground_truth."""
    items = []
    with open(test_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    print(f"Agent file:   {AGENT_FILE}")
    print(f"Test file:    {TEST_FILE}")
    print(f"Results file: {RESULTS_FILE}")
    print(f"Model:        {MODEL_NAME}")

    system_prompt = extract_system_prompt(AGENT_FILE)
    print(f"System prompt length: {len(system_prompt)} chars")

    test_items = load_test_items(TEST_FILE)
    print(f"Test items: {len(test_items)}")

    project_client = AIProjectClient(
        endpoint=ENDPOINT,
        credential=DefaultAzureCredential(),
    )
    client = project_client.get_openai_client()

    results = []
    for i, item in enumerate(test_items, 1):
        query = item["query"]
        ground_truth = item["ground_truth"]

        print(f"  [{i}/{len(test_items)}] {query[:80]}...")

        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ],
            temperature=0.2,
            max_tokens=512,
        )

        answer = response.choices[0].message.content.strip()
        results.append({
            "query": query,
            "response": answer,
            "ground_truth": ground_truth,
        })

    # Write results
    results_path = Path(RESULTS_FILE)
    results_path.parent.mkdir(parents=True, exist_ok=True)

    with open(results_path, "w", encoding="utf-8") as f:
        for item in results:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"\nWrote {len(results)} results to {RESULTS_FILE}")


if __name__ == "__main__":
    main()
