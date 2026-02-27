"""
reproduce_error.py - Reproduces the context length exceeded error.

Clones https://github.com/openclaw/openclaw and triggers analysis to
demonstrate the 400 error: context length exceeded.
"""

import os
import sys
import logging

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from github_utils import clone_github_repo
from scanner import scan_project, estimate_tokens

logging.basicConfig(
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
    format="%(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

GITHUB_URL = "https://github.com/openclaw/openclaw"
MAX_API_TOKENS = 1_048_576  # Actual API limit for Gemini 2.5 Flash Lite


def reproduce():
    """Clone openclaw and show how the prompt exceeds the API token limit."""
    print("=" * 70)
    print("  REPRODUCING: Context Length Exceeded Error")
    print("=" * 70)

    # Step 1: Clone
    print(f"\n[+] Cloning repository: {GITHUB_URL}")
    try:
        repo_dir = clone_github_repo(GITHUB_URL, force_redownload=False)
        print(f"[+] Repository ready at: {repo_dir}")
    except (ValueError, RuntimeError) as e:
        print(f"[-] Clone failed: {e}")
        sys.exit(1)

    # Step 2: Scan — this is what agent.analyze_project() calls
    print("\n[+] Scanning codebase (as agent.analyze_project does)...")
    result = scan_project(repo_dir)

    code_contents = result["code_contents"]
    directory_tree = result["directory_tree"]

    # Step 3: Build the exact prompt that agent.py sends to the API
    # (mirrors INITIAL_ANALYSIS_PROMPT.format(...) in agent.py)
    from agent import INITIAL_ANALYSIS_PROMPT, SYSTEM_PROMPT

    user_prompt = INITIAL_ANALYSIS_PROMPT.format(
        project_path=result["project_path"],
        directory_tree=directory_tree,
        code_contents=code_contents,
    )

    system_tokens = estimate_tokens(SYSTEM_PROMPT)
    user_tokens = estimate_tokens(user_prompt)
    total_tokens = system_tokens + user_tokens

    print(f"\n{'='*70}")
    print("  TOKEN ANALYSIS")
    print(f"{'='*70}")
    print(f"  System prompt tokens  : {system_tokens:>12,}")
    print(f"  User prompt tokens    : {user_tokens:>12,}")
    print(f"  TOTAL tokens          : {total_tokens:>12,}")
    print(f"  API max tokens        : {MAX_API_TOKENS:>12,}")
    print(f"  EXCEEDS LIMIT BY      : {max(0, total_tokens - MAX_API_TOKENS):>12,}")
    print(f"{'='*70}")

    if total_tokens > MAX_API_TOKENS:
        print(f"\n[!] ROOT CAUSE CONFIRMED:")
        print(f"    scan_project() returns {result['estimated_tokens']:,} tokens of code_contents.")
        print(f"    agent.analyze_project() passes ALL of it in one API call.")
        print(f"    The safe_token_limit={result['safe_token_limit']:,} in scanner.py is")
        print(f"    NEVER enforced — it only sets a warning flag (exceeds_limit={result['exceeds_limit']}).")
        print(f"    agent.py ignores the exceeds_limit flag entirely.")
        print(f"\n    This causes: Error code: 400 - context length exceeded")
        print(f"    ({total_tokens:,} tokens requested, max {MAX_API_TOKENS:,})")

        # Now actually trigger the API call to reproduce the real error
        print(f"\n[+] Triggering actual API call to reproduce the 400 error...")
        from agent import CodebaseAnalysisAgent
        agent = CodebaseAnalysisAgent()
        try:
            agent.analyze_project(repo_dir)
            print("[-] ERROR: Expected 400 error but call succeeded (unexpected)")
        except RuntimeError as e:
            print(f"\n[✓] ERROR REPRODUCED: {e}")
    else:
        print(f"\n[?] Total tokens ({total_tokens:,}) within limit — error may not reproduce.")


if __name__ == "__main__":
    reproduce()
