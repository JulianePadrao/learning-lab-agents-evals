"""
Human Evaluation App for Agent Responses

A Flask web app that:
1. Asks the evaluator for their name and which agent to evaluate
2. Loads pre-generated query/response pairs from results JSONL files
3. Presents a chat-style UI with a question list and grading sidebar
4. Allows revisiting and re-grading any question
5. Saves grades to reports/human_eval_results.csv

Environment variables (via .env):
    SECRET_KEY – Flask session signing key (optional, default provided for dev)
"""

import csv
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, redirect, render_template, request, session, url_for

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AGENTS_DIR = REPO_ROOT / "agents"
RESULTS_DIR = REPO_ROOT / "evaluation" / "data" / "technical-accuracy-results"
CSV_FILE = REPO_ROOT / "reports" / "human_eval_results.csv"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "human-eval-dev-key-change-in-prod")

_csv_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def list_agents() -> list[dict]:
    """Return available agents with filenames and display names."""
    agents = []
    for path in sorted(AGENTS_DIR.glob("*.agent.md")):
        display_name = path.stem.replace(".agent", "").replace("-", " ").title()
        agents.append({"filename": path.name, "display_name": display_name})
    return agents


def results_file_for_agent(agent_file: str | None) -> Path:
    """Map an agent filename to its results JSONL path."""
    if agent_file:
        agent_slug = agent_file.replace(".agent.md", "")
        return RESULTS_DIR / f"smoke-test-results-{agent_slug}.jsonl"
    return RESULTS_DIR / "smoke-test-results.jsonl"


def load_results(agent_file: str | None) -> list[dict]:
    """Load query/response pairs from the agent's results JSONL file."""
    path = results_file_for_agent(agent_file)
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


CSV_HEADERS = [
    "evaluator", "role", "agent", "prompt", "response",
    "factual_accuracy", "appropriate_level", "logical_progression",
    "helpfulness", "comments", "status", "timestamp",
]


def save_grade(evaluator: str, role: str, agent_file: str | None, prompt: str, response: str, grade: dict):
    """Append a grade row to the CSV file (thread-safe)."""
    CSV_FILE.parent.mkdir(parents=True, exist_ok=True)

    with _csv_lock:
        file_exists = CSV_FILE.exists() and CSV_FILE.stat().st_size > 0
        with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(CSV_HEADERS)
            writer.writerow([
                evaluator,
                role,
                agent_file or "(no agent)",
                prompt,
                response,
                grade.get("factual_accuracy", ""),
                grade.get("appropriate_level", ""),
                grade.get("logical_progression", ""),
                grade.get("helpfulness", ""),
                grade.get("comments", ""),
                grade.get("status", "graded"),
                datetime.now(timezone.utc).isoformat(),
            ])


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/", methods=["GET", "POST"])
def login():
    """Landing page — ask for name and agent, validate results file exists."""
    error = None
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        agent_file = request.form.get("agent_file", "").strip() or None
        role = request.form.get("role", "").strip()
        if name and role:
            results_path = results_file_for_agent(agent_file)
            if not results_path.exists():
                agent_display = agent_file.replace(".agent.md", "") if agent_file else "base model"
                error = f"No results file found for '{agent_display}'. Expected: {results_path.name}"
            else:
                session["evaluator"] = name
                session["role"] = role
                session["agent_file"] = agent_file
                session["index"] = 0
                session["grades"] = {}
                return redirect(url_for("evaluate"))
    return render_template("index.html", page="login", agents=list_agents(), error=error)


@app.route("/evaluate", methods=["GET", "POST"])
def evaluate():
    """Main evaluation page — show prompt + response, collect grade."""
    if "evaluator" not in session:
        return redirect(url_for("login"))

    agent_file = session.get("agent_file")
    results = load_results(agent_file)
    grades = session.get("grades", {})
    index = session.get("index", 0)

    # Clamp index
    if index < 0 or index >= len(results):
        index = 0
        session["index"] = 0

    # Handle grade submission
    if request.method == "POST":
        action = request.form.get("action", "save")
        current = results[index]

        if action == "skip":
            grades[str(index)] = {
                "factual_accuracy": "", "appropriate_level": "",
                "logical_progression": "", "helpfulness": "",
                "comments": "", "status": "skipped",
            }
        else:
            factual = request.form.get("factual_accuracy", "")
            try:
                appropriate = int(request.form.get("appropriate_level", 2))
                appropriate = max(1, min(3, appropriate))
            except (ValueError, TypeError):
                appropriate = 2
            try:
                logical = int(request.form.get("logical_progression", 2))
                logical = max(1, min(3, logical))
            except (ValueError, TypeError):
                logical = 2
            try:
                helpful = int(request.form.get("helpfulness", 2))
                helpful = max(1, min(3, helpful))
            except (ValueError, TypeError):
                helpful = 2
            comments = request.form.get("comments", "").strip()
            grade_data = {
                "factual_accuracy": factual,
                "appropriate_level": appropriate,
                "logical_progression": logical,
                "helpfulness": helpful,
                "comments": comments,
                "status": "graded",
            }
            grades[str(index)] = grade_data

        session["grades"] = grades

        # Auto-advance to next ungraded question, or stay if all done
        next_index = _next_ungraded(grades, len(results), index)
        session["index"] = next_index
        return redirect(url_for("evaluate"))

    # Handle navigation via query param
    nav = request.args.get("q")
    if nav is not None:
        try:
            nav_index = int(nav)
            if 0 <= nav_index < len(results):
                index = nav_index
                session["index"] = index
        except ValueError:
            pass

    current = results[index]

    # Build question list for left pane
    questions = []
    for i, item in enumerate(results):
        status = grades.get(str(i), {}).get("status", "pending")
        questions.append({
            "index": i,
            "query": item["query"],
            "status": status,
            "active": i == index,
        })

    agent_display = "Base Model"
    if agent_file:
        agent_display = agent_file.replace(".agent.md", "").replace("-", " ").title()

    # Pre-fill grade values if revisiting a graded question
    existing = grades.get(str(index), {})

    return render_template(
        "index.html",
        page="evaluate",
        evaluator=session["evaluator"],
        agent_name=agent_display,
        query=current["query"],
        response=current["response"],
        index=index,
        total=len(results),
        questions=questions,
        existing=existing,
        existing_status=existing.get("status", "pending"),
        graded_count=sum(1 for g in grades.values() if g.get("status") == "graded"),
        skipped_count=sum(1 for g in grades.values() if g.get("status") == "skipped"),
    )


def _next_ungraded(grades: dict, total: int, current: int) -> int:
    """Find the next ungraded question after current, wrapping around."""
    for offset in range(1, total + 1):
        candidate = (current + offset) % total
        if str(candidate) not in grades:
            return candidate
    return current


@app.route("/done")
def done():
    """Completion page — write all grades to CSV at once."""
    if "evaluator" not in session:
        return redirect(url_for("login"))

    agent_file = session.get("agent_file")
    results = load_results(agent_file)
    grades = session.get("grades", {})
    evaluator = session["evaluator"]
    role = session.get("role", "")

    # Auto-skip unanswered questions
    for i in range(len(results)):
        if str(i) not in grades:
            grades[str(i)] = {
                "factual_accuracy": "", "appropriate_level": "",
                "logical_progression": "", "helpfulness": "",
                "comments": "", "status": "skipped",
            }

    # Write all grades to CSV in one batch
    timestamp = datetime.now(timezone.utc).isoformat()
    for i, item in enumerate(results):
        grade = grades[str(i)]
        save_grade(evaluator, role, agent_file, item["query"], item["response"], grade)

    session["grades"] = grades

    return render_template(
        "index.html",
        page="done",
        evaluator=evaluator,
        csv_path=str(CSV_FILE),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Results dir: {RESULTS_DIR}")
    print(f"CSV output:  {CSV_FILE}")
    print(f"Agents dir:  {AGENTS_DIR}")
    print()
    app.run(debug=True, port=5000)
