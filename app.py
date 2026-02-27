"""
app.py - Flask web server for the Codebase Analysis Agent Terminal UI.

Serves a terminal-themed web interface that accepts a GitHub URL,
triggers the codebase download and analysis workflow, streams the
analysis report, and supports multi-turn QA about the codebase.
"""

import os
import re
import sys
import uuid
import json
import shutil
import logging
import threading
import time

from flask import Flask, request, jsonify, Response, send_from_directory
from dotenv import load_dotenv

# Load .env from the same directory as this file
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BASE_DIR, ".env"))

sys.path.insert(0, _BASE_DIR)

from agent import CodebaseAnalysisAgent
from github_utils import (
    clone_github_repo,
    DEFAULT_CLONE_BASE,
    GitHubRepoError,
    RepoNotFoundError,
    RepoPrivateError,
    RepoCloneError,
)

logging.basicConfig(
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
    format="%(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static", template_folder="templates")

# In-memory session store: session_id -> {agent, repo_dir}
_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()



# ── GitHub URL validation ──────────────────────────────────────────────────────

_GITHUB_URL_RE = re.compile(
    r"^(https?://)?(www\.)?github\.com/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+(\.git)?(/.*)?$",
    re.IGNORECASE,
)
_GITHUB_SSH_RE = re.compile(
    r"^git@github\.com:[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+(\.git)?$",
    re.IGNORECASE,
)


def is_valid_github_url(text: str) -> bool:
    """
    Return True if the given text is a recognisable GitHub repository URL.

    Accepts both HTTPS and SSH formats.

    Args:
        text: Raw user input string.

    Returns:
        True if the text looks like a valid GitHub repository URL.
    """
    text = text.strip()
    return bool(_GITHUB_URL_RE.match(text) or _GITHUB_SSH_RE.match(text))


# ── Session helpers ────────────────────────────────────────────────────────────

def _get_or_create_session(session_id: str) -> dict:
    """
    Retrieve an existing session record or create a new one.

    Args:
        session_id: Unique session identifier string.

    Returns:
        Dict with keys 'agent' (CodebaseAnalysisAgent) and 'repo_dir' (str|None).
    """
    with _sessions_lock:
        if session_id not in _sessions:
            _sessions[session_id] = {
                "agent": CodebaseAnalysisAgent(),
                "repo_dir": None,
            }
            logger.info("Created new agent session: %s", session_id)
        return _sessions[session_id]


def _clear_session(session_id: str) -> None:
    """
    Remove an agent session from the store and clean up its cloned repo.

    Args:
        session_id: Unique session identifier string.
    """
    with _sessions_lock:
        record = _sessions.pop(session_id, None)

    if record:
        repo_dir = record.get("repo_dir")
        if repo_dir and os.path.isdir(repo_dir):
            try:
                shutil.rmtree(repo_dir)
                logger.info("Deleted repo dir: %s", repo_dir)
            except OSError as e:
                logger.warning("Could not delete repo dir %s: %s", repo_dir, e)
        logger.info("Cleared session: %s", session_id)


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the main terminal UI HTML page."""
    return send_from_directory("templates", "index.html")


@app.route("/api/session/new", methods=["POST"])
def new_session():
    """
    Create a new analysis session and return its ID.

    Returns:
        JSON with 'session_id' key.
    """
    session_id = str(uuid.uuid4())
    _get_or_create_session(session_id)
    return jsonify({"session_id": session_id})


@app.route("/api/analyze", methods=["POST"])
def analyze():
    """
    Accept a GitHub URL, clone the repository, and stream the analysis report.

    Request JSON body:
        github_url  (str): GitHub repository URL to analyze.
        session_id  (str): Session identifier for context persistence.

    Returns:
        Server-Sent Events stream with analysis progress and report.
    """
    data = request.get_json(silent=True) or {}
    github_url = (data.get("github_url") or "").strip()
    session_id = (data.get("session_id") or "").strip()

    if not github_url:
        return jsonify({"error": "github_url is required"}), 400

    # Validate that the input is actually a GitHub URL
    if not is_valid_github_url(github_url):
        return jsonify({
            "error": (
                "Invalid input. Please provide a valid GitHub repository URL. "
                "Example: https://github.com/owner/repository"
            )
        }), 400

    if not session_id:
        session_id = str(uuid.uuid4())

    def generate():
        """Generator yielding SSE-formatted events for the analysis pipeline."""

        def sse(event: str, data_payload: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(data_payload)}\n\n"

        try:
            # Step 1: Clone repository
            yield sse("status", {"message": f"[+] Cloning repository: {github_url}", "step": "clone"})

            try:
                repo_dir = clone_github_repo(github_url, force_redownload=False)
            except RepoNotFoundError as e:
                yield sse("error", {
                    "message": f"[-] Repository not found: {str(e)}",
                    "error_type": "not_found",
                })
                return
            except RepoPrivateError as e:
                yield sse("error", {
                    "message": f"[-] Access denied: {str(e)}",
                    "error_type": "private",
                })
                return
            except RepoCloneError as e:
                yield sse("error", {
                    "message": f"[-] Clone failed: {str(e)}",
                    "error_type": "clone_error",
                })
                return
            except ValueError as e:
                yield sse("error", {
                    "message": f"[-] Invalid URL: {str(e)}",
                    "error_type": "invalid_url",
                })
                return
            except Exception as e:
                yield sse("error", {
                    "message": f"[-] Unexpected clone error: {str(e)}",
                    "error_type": "unknown",
                })
                return

            yield sse("status", {"message": f"[+] Repository ready at: {repo_dir}", "step": "clone_done"})

            # Step 2: Scan project
            yield sse("status", {"message": "[+] Scanning codebase structure...", "step": "scan"})

            # Step 3: Run LLM analysis via streaming generator
            record = _get_or_create_session(session_id)
            agent = record["agent"]
            agent.reset()  # Fresh analysis for new URL

            # Store repo_dir in session for cleanup on restart
            with _sessions_lock:
                if session_id in _sessions:
                    _sessions[session_id]["repo_dir"] = repo_dir

            # Stream granular pipeline progress events from the agent
            report = None
            try:
                for progress_event in agent.analyze_project_stream(repo_dir):
                    stage = progress_event.get("stage", "")
                    msg = progress_event.get("message", "")

                    if stage == "chunking":
                        yield sse("pipeline", {
                            "stage": "chunking",
                            "message": msg,
                            "total_chunks": progress_event.get("total_chunks", 1),
                            "step": "chunking",
                        })

                    elif stage == "mapping":
                        yield sse("pipeline", {
                            "stage": "mapping",
                            "message": msg,
                            "chunk": progress_event.get("chunk", 0),
                            "total": progress_event.get("total", 1),
                            "step": "mapping",
                        })

                    elif stage == "reducing":
                        yield sse("pipeline", {
                            "stage": "reducing",
                            "message": msg,
                            "round": progress_event.get("round", 1),
                            "batch": progress_event.get("batch", 0),
                            "total_batches": progress_event.get("total_batches", 1),
                            "step": "reducing",
                        })

                    elif stage == "report":
                        report = progress_event.get("report", "")

                    elif stage == "error":
                        yield sse("error", {"message": msg})
                        return

            except RuntimeError as e:
                yield sse("error", {"message": f"[-] Analysis failed: {str(e)}"})
                return

            if not report:
                yield sse("error", {"message": "[-] Analysis produced no report."})
                return

            # Step 4: Stream the final report
            yield sse("report", {
                "message": "[+] Analysis complete.",
                "report": report,
                "session_id": session_id,
                "repo_dir": repo_dir,
                "step": "report_done",
            })

        except Exception as e:
            logger.exception("Unexpected error in analyze stream")
            yield sse("error", {"message": f"[-] Unexpected error: {str(e)}"})

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/ask", methods=["POST"])
def ask():
    """
    Answer a follow-up question about the analyzed codebase.

    Request JSON body:
        question    (str): The user's follow-up question.
        session_id  (str): Session identifier for context retrieval.

    Returns:
        JSON with 'answer' key, or error details.
    """
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    session_id = (data.get("session_id") or "").strip()

    if not question:
        return jsonify({"error": "question is required"}), 400

    if not session_id:
        return jsonify({"error": "session_id is required"}), 400

    with _sessions_lock:
        record = _sessions.get(session_id)

    if record is None:
        return jsonify({"error": "Session not found. Please analyze a repository first."}), 404

    agent = record["agent"]

    if not agent.conversation_history:
        return jsonify({"error": "No codebase analyzed in this session yet."}), 400

    try:
        answer = agent.ask(question)
        return jsonify({"answer": answer, "session_id": session_id})
    except RuntimeError as e:
        logger.error("Ask failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/session/reset", methods=["POST"])
def reset_session():
    """
    Reset or clear an existing session to start fresh.

    Request JSON body:
        session_id (str): Session identifier to reset.

    Returns:
        JSON confirmation with new session_id.
    """
    data = request.get_json(silent=True) or {}
    session_id = (data.get("session_id") or "").strip()

    if session_id:
        _clear_session(session_id)

    new_id = str(uuid.uuid4())
    _get_or_create_session(new_id)
    return jsonify({"session_id": new_id, "message": "Session reset."})


@app.route("/api/restart", methods=["POST"])
def restart_session():
    """
    Fully restart the session: clear state, delete cloned repo, issue new session ID.

    Request JSON body:
        session_id (str): Current session identifier to destroy.

    Returns:
        JSON with new 'session_id' and confirmation message.
    """
    data = request.get_json(silent=True) or {}
    session_id = (data.get("session_id") or "").strip()

    if session_id:
        _clear_session(session_id)
        logger.info("Restart requested for session: %s", session_id)

    new_id = str(uuid.uuid4())
    _get_or_create_session(new_id)
    logger.info("New session after restart: %s", new_id)

    return jsonify({
        "session_id": new_id,
        "message": "Session restarted. Ready for a new GitHub URL.",
    })


@app.route("/api/config")
def get_config():
    """
    Return public runtime configuration for the frontend.

    Returns:
        JSON with 'model' key reflecting the active MODEL_NAME from .env.
    """
    return jsonify({"model": os.getenv("MODEL_NAME", "google/gemini-2.5-flash-lite")})


@app.route("/health")
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok", "model": os.getenv("MODEL_NAME", "google/gemini-2.5-flash-lite")})


if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    logger.info("Starting Codebase Analysis Agent UI on http://%s:%d", host, port)
    app.run(host=host, port=port, debug=debug, threaded=True)
