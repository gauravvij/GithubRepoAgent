"""
agent.py - AI-powered codebase analysis agent.

Uses the OpenRouter API with Gemini 2.5 Flash Lite to analyze project codebases,
generate comprehensive reports, and answer follow-up questions in a stateful
multi-turn conversation.

Configuration is loaded from a .env file via python-dotenv.

Context-limit enforcement
-------------------------
Gemini 2.5 Flash Lite (via OpenRouter) has a hard limit of 1,048,576 tokens per
request.  For large codebases the raw code_contents can far exceed this limit.

The agent enforces the limit with a two-stage strategy:

1. **Fit-in-one** – if the full prompt (system + user) fits within
MAX_PROMPT_TOKENS, it is sent as a single request (original behaviour).

2. **Map-Reduce** – if the content is too large, the code_contents are split
into chunks that each fit within CHUNK_TOKEN_BUDGET tokens.  Each chunk is
analysed independently (map phase), and the resulting partial summaries are
combined into one final synthesis call (reduce phase).

Hierarchical Reduce Optimisation
---------------------------------
Instead of feeding all N chunk summaries into a single reduce call (which can
produce a very large payload for repos with 20+ chunks), the reduce phase now
works in parallel batches:

  1. Summaries are grouped into batches of REDUCE_BATCH_SIZE.
  2. Each batch is reduced in parallel via ThreadPoolExecutor → intermediate
     summaries.
  3. If more than one intermediate summary exists, the process repeats
     recursively until a single final summary remains.

This keeps every individual API call well within the context limit and
dramatically cuts wall-clock time for large repos.

Granular Progress Streaming
----------------------------
analyze_project_stream() is a generator that yields progress dicts at each
discrete pipeline stage so the web UI can display live feedback:

  {"stage": "chunking",  "message": "...", ...}
  {"stage": "mapping",   "message": "...", "chunk": N, "total": M}
  {"stage": "reducing",  "message": "...", "batch": N, "total": M}
  {"stage": "report",    "message": "...", "report": "<full text>"}
  {"stage": "error",     "message": "..."}
"""

import os
import sys
import logging
from typing import Optional, Generator
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from openai import OpenAI

from scanner import scan_project, estimate_tokens, chunk_text

# Load environment variables from .env file
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

logging.basicConfig(
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
    format="%(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

# OpenRouter configuration — loaded from .env
OPENROUTER_API_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
MODEL_NAME = os.getenv("MODEL_NAME", "google/gemini-2.5-flash-lite")

# ── Token-limit constants ──────────────────────────────────────────────────────
# Hard API limit for Gemini 2.5 Flash Lite via OpenRouter.
API_MAX_TOKENS: int = 1_048_576

# Hard limit for 128K context window models (e.g. gpt-oss-20b).
# Used to compute a dynamic per-chunk code budget that accounts for the
# actual MAP_CHUNK_PROMPT overhead (system prompt + directory tree + template).
# The dynamic budget is computed in analyze_project_stream() before chunking.
# This constant is the fallback / default when no dynamic computation is done.
MODEL_CONTEXT_LIMIT: int = 131_072    # hard limit for 128K models

# We reserve headroom for the system prompt, prompt template boilerplate, and
# the model's own output budget.  The remaining budget is available for code.
PROMPT_OVERHEAD_TOKENS: int = 8_000   # system prompt + template text + output
# MAX_PROMPT_TOKENS is the threshold for single-call vs map-reduce routing.
# It must be based on MODEL_CONTEXT_LIMIT (131,072) — the actual hard limit
# of the deployed model — NOT on API_MAX_TOKENS (1,048,576) which is a
# platform-level ceiling that does not reflect the model's real context window.
# Using API_MAX_TOKENS here caused the agent to send ~494k-token prompts as
# single calls, resulting in HTTP 400 "maximum context length is 131072" errors.
MAX_PROMPT_TOKENS: int = MODEL_CONTEXT_LIMIT - PROMPT_OVERHEAD_TOKENS  # ~123 072

# Safety headroom reserved for model output tokens and any estimation error.
CHUNK_SAFETY_MARGIN: int = 8_000

# Default chunk budget (used as fallback). Will be overridden dynamically
# in analyze_project_stream() based on actual prompt overhead.
CHUNK_TOKEN_BUDGET: int = 60_000      # conservative fallback

# Hierarchical reduce: token-based aggregation threshold.
# Summaries are accumulated into a batch until their combined token count
# approaches this limit. This guarantees every reduce API call stays well
# under the 128k context window regardless of how many chunks exist.
# We use 90,000 tokens as the safe ceiling (leaving headroom for system
# prompt, template boilerplate, and model output budget).
REDUCE_TOKEN_BUDGET: int = 90_000

# Hard cap on summaries per batch as a safety backstop (prevents runaway
# batches if token estimates are inaccurate).
REDUCE_BATCH_MAX: int = 20

# System prompt for the codebase analysis agent
SYSTEM_PROMPT = """You are an expert software architect and code analyst. Your role is to analyze
project codebases and provide detailed, accurate insights.

When given a codebase to analyze, you will:
1. Identify the project's overall purpose and architecture
2. Map out the directory structure and explain the organization rationale
3. Describe each file's purpose and responsibility
4. Identify key components, modules, and their interactions
5. Trace import/dependency relationships between files
6. Identify entry points and describe data flow
7. Recognize architectural patterns (MVC, microservices, layered, etc.)
8. Note any configuration files and their roles

When answering follow-up questions:
- Reference specific files and line-level details from the codebase context
- Provide concrete, accurate answers based solely on the analyzed code
- Trace execution paths and data flows when asked
- Highlight relationships between components clearly

Always be precise, technical, and grounded in the actual code provided."""

INITIAL_ANALYSIS_PROMPT = """Please analyze the following project codebase and generate a comprehensive report.

## Project Path
{project_path}

## Directory Structure
```
{directory_tree}
```

## File Contents
{code_contents}

---

Please provide a structured analysis report with the following sections:

### 1. Project Overview
Brief description of what this project does and its overall purpose.

### 2. Directory Structure & Organization
Explain the directory hierarchy and the rationale behind the organization.

### 3. File Inventory & Purposes
For each file, describe:
- Its primary responsibility
- Key functions/classes/exports it contains
- Its role in the overall system

### 4. Component Interactions & Relationships
How do the different modules/files interact with each other?
Which components depend on which?

### 5. Dependency Map
List all import/dependency relationships between files (internal dependencies).
Also note any external libraries/packages used.

### 6. Entry Points & Data Flow
Identify the main entry points of the application.
Describe how data flows through the system from input to output.

### 7. Architecture Patterns
What architectural patterns or design principles are evident in this codebase?

### 8. Summary
A concise summary of the codebase's strengths and overall design quality."""

# Prompt used during the map phase to summarise one chunk of code files
MAP_CHUNK_PROMPT = """You are analysing a large codebase in parts. This is chunk {chunk_index} of {total_chunks}.

## Project Path
{project_path}

## Directory Structure (full project)
```
{directory_tree}
```

## File Contents (this chunk only)
{code_contents}

---

Provide a concise technical summary of the files in this chunk covering:
- File names and their primary responsibilities
- Key classes, functions, and exports
- Import/dependency relationships visible in this chunk
- Any entry points or notable patterns

Be thorough but concise — this summary will be merged with summaries of other chunks."""

# Prompt used during the reduce phase to synthesise a batch of summaries
REDUCE_BATCH_PROMPT = """You are synthesising partial codebase summaries into a consolidated intermediate summary.

## Project Path
{project_path}

## Directory Structure
```
{directory_tree}
```

## Partial Summaries to Consolidate
{chunk_summaries}

---

Produce a consolidated technical summary that preserves all key information:
- All file names and their responsibilities
- All key classes, functions, and exports
- All import/dependency relationships
- All entry points and notable patterns

Be comprehensive but avoid redundancy."""

# Prompt used during the final reduce phase to synthesise all intermediate summaries
REDUCE_SYNTHESIS_PROMPT = """You have analysed a large codebase in {total_chunks} parts.
Below are the consolidated summaries. Synthesise them into one comprehensive analysis report.

## Project Path
{project_path}

## Directory Structure
```
{directory_tree}
```

## Consolidated Summaries
{chunk_summaries}

---

Please provide a complete structured analysis report with the following sections:

### 1. Project Overview
Brief description of what this project does and its overall purpose.

### 2. Directory Structure & Organization
Explain the directory hierarchy and the rationale behind the organization.

### 3. File Inventory & Purposes
For each file (across all chunks), describe its primary responsibility and role.

### 4. Component Interactions & Relationships
How do the different modules/files interact with each other?
Which components depend on which?

### 5. Dependency Map
List all import/dependency relationships between files (internal dependencies).
Also note any external libraries/packages used.

### 6. Entry Points & Data Flow
Identify the main entry points of the application.
Describe how data flows through the system from input to output.

### 7. Architecture Patterns
What architectural patterns or design principles are evident in this codebase?

### 8. Summary
A concise summary of the codebase's strengths and overall design quality."""


class CodebaseAnalysisAgent:
    """
    Stateful agent for analyzing project codebases using an LLM.

    Maintains conversation history to support multi-turn interactions
    while preserving the initial codebase context throughout the session.

    For large codebases that exceed the API context limit, a hierarchical
    map-reduce strategy is used:
      - Map phase: each chunk is summarised independently in parallel.
      - Reduce phase: summaries are batched and reduced in parallel layers
        until a single consolidated summary remains, which is then used for
        the final synthesis call.

    Progress is streamed via analyze_project_stream() which yields dicts
    describing each pipeline stage for real-time UI feedback.
    """

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize the agent with OpenRouter API credentials.

        Credentials are resolved in this order:
        1. Explicit api_key argument
        2. OPENROUTER_API_KEY environment variable (loaded from .env)

        Args:
            api_key: Optional override for the OpenRouter API key.
        """
        resolved_key = api_key or OPENROUTER_API_KEY
        if not resolved_key:
            raise ValueError(
                "OpenRouter API key not found. Set OPENROUTER_API_KEY in .env or pass api_key."
            )

        self.client = OpenAI(
            base_url=OPENROUTER_API_URL,
            api_key=resolved_key,
        )
        self.model = MODEL_NAME
        self.conversation_history: list[dict] = []
        self.project_path: Optional[str] = None
        self.scan_result: Optional[dict] = None
        logger.info("CodebaseAnalysisAgent initialized with model: %s", self.model)

    def _call_llm(self, messages: list[dict]) -> str:
        """
        Make a call to the LLM via OpenRouter API.

        Performs a pre-flight token estimate and raises a clear error if the
        payload would exceed the API hard limit, preventing a 400 response.

        Args:
            messages: List of message dicts with 'role' and 'content' keys.

        Returns:
            The assistant's response text.

        Raises:
            RuntimeError: If the API call fails or the payload is too large.
        """
        total_text = " ".join(m.get("content", "") for m in messages)
        estimated = estimate_tokens(total_text)
        if estimated > MODEL_CONTEXT_LIMIT:
            raise RuntimeError(
                f"Payload too large: estimated ~{estimated:,} tokens exceeds "
                f"the model context limit of {MODEL_CONTEXT_LIMIT:,} tokens. "
                "Use map-reduce chunking for large codebases."
            )
        logger.debug("Pre-flight token estimate: ~%d tokens", estimated)

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error("LLM API call failed: %s", e)
            raise RuntimeError(f"LLM API call failed: {e}") from e

    # ── Map phase ──────────────────────────────────────────────────────────────

    def _map_chunk(
        self,
        chunk_index: int,
        total_chunks: int,
        project_path: str,
        directory_tree: str,
        code_contents_chunk: str,
    ) -> str:
        """
        Analyse one chunk of code files and return a concise summary.

        Args:
            chunk_index:         1-based index of this chunk.
            total_chunks:        Total number of chunks.
            project_path:        Absolute path of the project root.
            directory_tree:      Full directory tree string.
            code_contents_chunk: The subset of code file contents for this chunk.

        Returns:
            A text summary of the files in this chunk.
        """
        prompt = MAP_CHUNK_PROMPT.format(
            chunk_index=chunk_index,
            total_chunks=total_chunks,
            project_path=project_path,
            directory_tree=directory_tree,
            code_contents=code_contents_chunk,
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        logger.info(
            "Map phase: analysing chunk %d/%d (~%d tokens)...",
            chunk_index, total_chunks, estimate_tokens(prompt),
        )
        return self._call_llm(messages)

    # ── Hierarchical Reduce phase ──────────────────────────────────────────────

    def _reduce_batch(
        self,
        batch: list[str],
        batch_index: int,
        total_batches: int,
        project_path: str,
        directory_tree: str,
    ) -> str:
        """
        Reduce a single batch of summaries into one consolidated intermediate summary.

        Args:
            batch:         List of summary strings to consolidate.
            batch_index:   1-based index of this batch (for logging).
            total_batches: Total number of batches in this reduce round.
            project_path:  Absolute path of the project root.
            directory_tree: Full directory tree string.

        Returns:
            A consolidated summary string.
        """
        combined = "\n\n---\n\n".join(
            f"### Summary {i + 1}\n{s}" for i, s in enumerate(batch)
        )
        prompt = REDUCE_BATCH_PROMPT.format(
            project_path=project_path,
            directory_tree=directory_tree,
            chunk_summaries=combined,
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        logger.info(
            "Reduce batch %d/%d: consolidating %d summaries (~%d tokens)...",
            batch_index, total_batches, len(batch), estimate_tokens(prompt),
        )
        return self._call_llm(messages)

    def _build_token_aware_batches(self, summaries: list[str]) -> list[list[str]]:
        """
        Group summaries into batches whose combined token count stays under
        REDUCE_TOKEN_BUDGET.

        Each batch accumulates summaries until adding the next one would push
        the combined token count over the budget, or until REDUCE_BATCH_MAX
        summaries have been collected.  This guarantees every reduce API call
        stays well within the 128k context window regardless of summary size.

        Args:
            summaries: List of summary strings to batch.

        Returns:
            List of batches, where each batch is a list of summary strings.
        """
        # Overhead tokens: system prompt + REDUCE_BATCH_PROMPT template text
        overhead = estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(
            REDUCE_BATCH_PROMPT.format(
                project_path="", directory_tree="", chunk_summaries=""
            )
        )
        available = REDUCE_TOKEN_BUDGET - overhead

        batches: list[list[str]] = []
        current_batch: list[str] = []
        current_tokens: int = 0

        for summary in summaries:
            s_tokens = estimate_tokens(summary)
            # If a single summary already exceeds the budget, it must go alone
            if current_batch and (
                current_tokens + s_tokens > available
                or len(current_batch) >= REDUCE_BATCH_MAX
            ):
                batches.append(current_batch)
                current_batch = []
                current_tokens = 0
            current_batch.append(summary)
            current_tokens += s_tokens

        if current_batch:
            batches.append(current_batch)

        return batches

    def _hierarchical_reduce(
        self,
        project_path: str,
        directory_tree: str,
        summaries: list[str],
        progress_callback=None,
        reduce_round: int = 1,
    ) -> str:
        """
        Recursively reduce a list of summaries using parallel token-aware batches.

        Instead of fixed-size batches, summaries are grouped dynamically so
        that the combined token count of each batch stays under REDUCE_TOKEN_BUDGET.
        This prevents 128k context-length errors regardless of summary size or
        the number of chunks.

        Each batch is reduced in parallel via ThreadPoolExecutor. If more than
        one intermediate summary results, the process repeats recursively until
        a single summary remains.

        Args:
            project_path:      Absolute path of the project root.
            directory_tree:    Full directory tree string.
            summaries:         List of summary strings to reduce.
            progress_callback: Optional callable(dict) for progress events.
            reduce_round:      Current recursion depth (for logging).

        Returns:
            A single consolidated summary string ready for final synthesis.
        """
        if len(summaries) == 1:
            return summaries[0]

        # Build token-aware batches (dynamic, not fixed-size)
        batches = self._build_token_aware_batches(summaries)
        total_batches = len(batches)

        # Log token estimates for first 2-3 batches to aid debugging
        for i, batch in enumerate(batches[:3]):
            combined_tokens = sum(estimate_tokens(s) for s in batch)
            logger.info(
                "Reduce round %d, batch %d/%d: %d summaries, ~%d combined tokens "
                "(budget=%d).",
                reduce_round, i + 1, total_batches,
                len(batch), combined_tokens, REDUCE_TOKEN_BUDGET,
            )

        logger.info(
            "Reduce round %d: %d summaries → %d token-aware batches "
            "(budget=%d tokens each).",
            reduce_round, len(summaries), total_batches, REDUCE_TOKEN_BUDGET,
        )

        if progress_callback:
            progress_callback({
                "stage": "reducing",
                "message": (
                    f"[+] Reduce round {reduce_round}: consolidating "
                    f"{len(summaries)} summaries into {total_batches} batch(es)..."
                ),
                "round": reduce_round,
                "total_summaries": len(summaries),
                "total_batches": total_batches,
                "batch": 0,
            })

        intermediate: list[str] = [""] * total_batches
        max_workers = min(total_batches, 8)

        def _batch_task(args):
            """Worker: reduce one batch and return (index, result)."""
            idx, batch = args
            result = self._reduce_batch(
                batch=batch,
                batch_index=idx + 1,
                total_batches=total_batches,
                project_path=project_path,
                directory_tree=directory_tree,
            )
            return idx, result

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_batch_task, (i, batch)): i
                for i, batch in enumerate(batches)
            }
            completed = 0
            for future in as_completed(futures):
                idx, result = future.result()
                intermediate[idx] = result
                completed += 1
                logger.info(
                    "Reduce round %d: batch %d/%d complete.",
                    reduce_round, idx + 1, total_batches,
                )
                if progress_callback:
                    progress_callback({
                        "stage": "reducing",
                        "message": (
                            f"[+] Reduce round {reduce_round}: "
                            f"batch {completed}/{total_batches} complete."
                        ),
                        "round": reduce_round,
                        "batch": completed,
                        "total_batches": total_batches,
                    })

        # Recurse if we still have more than one intermediate summary
        if len(intermediate) > 1:
            return self._hierarchical_reduce(
                project_path=project_path,
                directory_tree=directory_tree,
                summaries=intermediate,
                progress_callback=progress_callback,
                reduce_round=reduce_round + 1,
            )

        return intermediate[0]

    def _final_synthesis(
        self,
        project_path: str,
        directory_tree: str,
        consolidated_summary: str,
        total_original_chunks: int,
    ) -> str:
        """
        Produce the final comprehensive report from the consolidated summary.

        Args:
            project_path:          Absolute path of the project root.
            directory_tree:        Full directory tree string.
            consolidated_summary:  Single consolidated summary from hierarchical reduce.
            total_original_chunks: Number of original map-phase chunks (for context).

        Returns:
            The final comprehensive analysis report string.
        """
        prompt = REDUCE_SYNTHESIS_PROMPT.format(
            total_chunks=total_original_chunks,
            project_path=project_path,
            directory_tree=directory_tree,
            chunk_summaries=consolidated_summary,
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        logger.info(
            "Final synthesis: generating report from consolidated summary (~%d tokens)...",
            estimate_tokens(prompt),
        )
        return self._call_llm(messages)

    # ── Streaming public API ───────────────────────────────────────────────────

    def analyze_project_stream(
        self, project_dir: str
    ) -> Generator[dict, None, None]:
        """
        Scan the project and stream granular progress events as a generator.

        Each yielded dict has at minimum a 'stage' key with one of:
          'chunking'  – codebase has been split into chunks
          'mapping'   – a map-phase chunk has completed
          'reducing'  – a reduce-phase batch has completed
          'report'    – final report is ready (includes 'report' key)
          'error'     – an error occurred (includes 'message' key)

        Args:
            project_dir: Path to the project directory to analyze.

        Yields:
            Progress dicts describing each pipeline stage.
        """
        logger.info("Starting streaming project analysis for: %s", project_dir)

        try:
            # ── Scan ──────────────────────────────────────────────────────────
            self.scan_result = scan_project(project_dir)
            self.project_path = self.scan_result["project_path"]
            directory_tree = self.scan_result["directory_tree"]
            code_contents = self.scan_result["code_contents"]
            project_path = self.scan_result["project_path"]

            # Estimate tokens for the full prompt
            full_user_prompt = INITIAL_ANALYSIS_PROMPT.format(
                project_path=project_path,
                directory_tree=directory_tree,
                code_contents=code_contents,
            )
            system_tokens = estimate_tokens(SYSTEM_PROMPT)
            user_tokens = estimate_tokens(full_user_prompt)
            total_tokens = system_tokens + user_tokens

            logger.info(
                "Prompt token estimate: system=%d, user=%d, total=%d (API limit=%d)",
                system_tokens, user_tokens, total_tokens, API_MAX_TOKENS,
            )

            if total_tokens <= MAX_PROMPT_TOKENS:
                # ── Strategy 1: single call ────────────────────────────────
                logger.info("Using single-call strategy (fits within context window).")
                yield {
                    "stage": "chunking",
                    "message": "[+] Codebase fits in context window — using single-call strategy.",
                    "total_chunks": 1,
                }
                self.conversation_history = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": full_user_prompt},
                ]
                yield {
                    "stage": "mapping",
                    "message": "[+] Mapping: analysing full codebase in one pass...",
                    "chunk": 1,
                    "total": 1,
                }
                logger.info("Calling LLM for initial codebase analysis...")
                report = self._call_llm(self.conversation_history)
                self.conversation_history.append(
                    {"role": "assistant", "content": report}
                )

            else:
                # ── Strategy 2: hierarchical map-reduce ────────────────────
                logger.info(
                    "Content too large (%d tokens > %d limit). Using hierarchical map-reduce.",
                    total_tokens, MAX_PROMPT_TOKENS,
                )

                # Chunking phase
                # Compute the actual MAP prompt overhead using the real directory
                # tree and project path so the chunk budget is always accurate,
                # even for repos with very large directory trees.
                map_prompt_overhead = estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(
                    MAP_CHUNK_PROMPT.format(
                        chunk_index=1,
                        total_chunks=1,  # placeholder; tree dominates
                        project_path=project_path,
                        directory_tree=directory_tree,
                        code_contents="",
                    )
                )
                dynamic_chunk_budget = max(
                    1_000,  # absolute minimum to avoid infinite loops
                    MODEL_CONTEXT_LIMIT - map_prompt_overhead - CHUNK_SAFETY_MARGIN,
                )
                logger.info(
                    "Dynamic chunk budget: %d tokens "
                    "(context_limit=%d - overhead=%d - margin=%d).",
                    dynamic_chunk_budget,
                    MODEL_CONTEXT_LIMIT,
                    map_prompt_overhead,
                    CHUNK_SAFETY_MARGIN,
                )
                chunks = chunk_text(code_contents, dynamic_chunk_budget)
                total_chunks = len(chunks)
                logger.info(
                    "Split codebase into %d chunks for parallel map phase.", total_chunks
                )
                yield {
                    "stage": "chunking",
                    "message": (
                        f"[+] Chunking: split codebase into {total_chunks} chunk(s) "
                        f"of ~{dynamic_chunk_budget:,} tokens each."
                    ),
                    "total_chunks": total_chunks,
                }

                # Map phase: summarise all chunks concurrently
                max_workers = min(total_chunks, 8)
                chunk_summaries: list[str] = [""] * total_chunks
                map_completed = [0]  # mutable counter for thread-safe increment

                # Collect progress events from threads via a queue
                import queue as _queue
                progress_q: _queue.Queue = _queue.Queue()

                def _map_task(idx_chunk):
                    """Worker: analyse one chunk and return (index, summary)."""
                    idx, chunk = idx_chunk
                    summary = self._map_chunk(
                        chunk_index=idx + 1,
                        total_chunks=total_chunks,
                        project_path=project_path,
                        directory_tree=directory_tree,
                        code_contents_chunk=chunk,
                    )
                    return idx, summary

                logger.info(
                    "Launching %d parallel workers for map phase (%d chunks).",
                    max_workers, total_chunks,
                )
                yield {
                    "stage": "mapping",
                    "message": (
                        f"[+] Mapping: launching {max_workers} parallel worker(s) "
                        f"for {total_chunks} chunk(s)..."
                    ),
                    "chunk": 0,
                    "total": total_chunks,
                }

                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(_map_task, (i, chunk)): i
                        for i, chunk in enumerate(chunks)
                    }
                    for future in as_completed(futures):
                        idx, summary = future.result()
                        chunk_summaries[idx] = summary
                        map_completed[0] += 1
                        logger.info(
                            "Map phase: chunk %d/%d complete.",
                            idx + 1, total_chunks,
                        )
                        yield {
                            "stage": "mapping",
                            "message": (
                                f"[+] Mapping: chunk {map_completed[0]}/{total_chunks} complete."
                            ),
                            "chunk": map_completed[0],
                            "total": total_chunks,
                        }

                logger.info(
                    "All %d map-phase chunks completed in parallel.", total_chunks
                )

                # Reduce phase: hierarchical batched reduction
                reduce_events: list[dict] = []

                def _reduce_progress(event: dict) -> None:
                    """Collect reduce progress events for yielding."""
                    reduce_events.append(event)

                # We run the hierarchical reduce synchronously but collect
                # events via callback so we can yield them after each step.
                # For true streaming during reduce, we use a thread + queue approach.
                reduce_q: _queue.Queue = _queue.Queue()

                def _reduce_progress_q(event: dict) -> None:
                    """Push reduce progress events into the queue."""
                    reduce_q.put(event)

                import threading as _threading

                reduce_result: list = [None]
                reduce_error: list = [None]

                def _run_reduce():
                    """Run hierarchical reduce in a background thread."""
                    try:
                        consolidated = self._hierarchical_reduce(
                            project_path=project_path,
                            directory_tree=directory_tree,
                            summaries=chunk_summaries,
                            progress_callback=_reduce_progress_q,
                        )
                        final = self._final_synthesis(
                            project_path=project_path,
                            directory_tree=directory_tree,
                            consolidated_summary=consolidated,
                            total_original_chunks=total_chunks,
                        )
                        reduce_result[0] = final
                    except Exception as exc:
                        reduce_error[0] = exc
                    finally:
                        reduce_q.put(None)  # sentinel

                reduce_thread = _threading.Thread(target=_run_reduce, daemon=True)
                reduce_thread.start()

                # Stream reduce progress events as they arrive
                while True:
                    event = reduce_q.get()
                    if event is None:
                        break
                    yield event

                reduce_thread.join()

                if reduce_error[0] is not None:
                    raise reduce_error[0]

                report = reduce_result[0]

                # Store compact context for follow-up questions
                self.conversation_history = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"I have analysed the codebase at {project_path}. "
                            "Here is the full analysis report. Please use it to answer "
                            "any follow-up questions I ask.\n\n"
                            f"## Directory Structure\n```\n{directory_tree}\n```\n\n"
                            f"## Analysis Report\n{report}"
                        ),
                    },
                    {
                        "role": "assistant",
                        "content": (
                            "Understood. I have reviewed the full analysis report and the "
                            "directory structure. I'm ready to answer any follow-up questions "
                            "about this codebase."
                        ),
                    },
                ]
                logger.info("Hierarchical map-reduce analysis complete.")

            # ── Emit final report ──────────────────────────────────────────
            yield {
                "stage": "report",
                "message": "[+] Analysis complete.",
                "report": report,
            }

        except Exception as exc:
            logger.error("analyze_project_stream error: %s", exc)
            yield {
                "stage": "error",
                "message": f"[-] Analysis failed: {exc}",
            }

    def analyze_project(self, project_dir: str) -> str:
        """
        Scan the project directory and generate an initial comprehensive analysis report.

        Convenience wrapper around analyze_project_stream() that blocks until
        the report is ready and returns it as a string.

        Args:
            project_dir: Path to the project directory to analyze.

        Returns:
            The comprehensive codebase analysis report as a string.

        Raises:
            RuntimeError: If the analysis fails.
        """
        report = None
        for event in self.analyze_project_stream(project_dir):
            if event.get("stage") == "report":
                report = event["report"]
            elif event.get("stage") == "error":
                raise RuntimeError(event["message"])
        if report is None:
            raise RuntimeError("Analysis completed without producing a report.")
        return report

    # ── Q&A ───────────────────────────────────────────────────────────────────

    def ask(self, question: str) -> str:
        """
        Ask a follow-up question about the analyzed codebase.

        Maintains full conversation history so the LLM has context from
        the initial analysis and all prior exchanges.

        Args:
            question: The user's follow-up question about the codebase.

        Returns:
            The LLM's contextual answer as a string.

        Raises:
            RuntimeError: If no project has been analyzed yet.
        """
        if not self.conversation_history:
            raise RuntimeError(
                "No project has been analyzed yet. Call analyze_project() first."
            )

        self.conversation_history.append({"role": "user", "content": question})

        logger.info("Calling LLM for follow-up question...")
        answer = self._call_llm(self.conversation_history)

        self.conversation_history.append({"role": "assistant", "content": answer})

        logger.info("Follow-up answer complete.")
        return answer

    def reset(self) -> None:
        """
        Reset the agent state, clearing conversation history and scan results.

        Useful for starting a fresh analysis session without creating a new agent.
        """
        self.conversation_history = []
        self.project_path = None
        self.scan_result = None
        logger.info("Agent state reset.")

    def chat_loop(self, project_dir: str) -> None:
        """
        Run an interactive multi-turn chat session for codebase analysis.

        First analyzes the project, then enters a loop accepting user questions
        until the user types 'exit' or 'quit'.

        Args:
            project_dir: Path to the project directory to analyze.
        """
        print("\n" + "=" * 70)
        print("  CODEBASE ANALYSIS AGENT")
        print("=" * 70)
        print(f"Analyzing project: {project_dir}")
        print("Please wait...\n")

        report = self.analyze_project(project_dir)
        print("\n" + "=" * 70)
        print("  INITIAL CODEBASE ANALYSIS REPORT")
        print("=" * 70)
        print(report)
        print("\n" + "=" * 70)

        print("\nYou can now ask follow-up questions about the codebase.")
        print("Type 'exit' or 'quit' to end the session.\n")

        while True:
            try:
                user_input = input("Your question: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nSession ended.")
                break

            if not user_input:
                continue

            if user_input.lower() in {"exit", "quit"}:
                print("Session ended.")
                break

            print("\nAgent response:")
            print("-" * 50)
            answer = self.ask(user_input)
            print(answer)
            print("-" * 50 + "\n")


def main():
    """
    CLI entry point for the codebase analysis agent.

    Usage:
        python agent.py <project_directory>
    """
    if len(sys.argv) < 2:
        print("Usage: python agent.py <project_directory>")
        print("Example: python agent.py ./my_project")
        sys.exit(1)

    project_dir = sys.argv[1]

    if not os.path.isdir(project_dir):
        print(f"Error: '{project_dir}' is not a valid directory.")
        sys.exit(1)

    agent = CodebaseAnalysisAgent()
    agent.chat_loop(project_dir)


if __name__ == "__main__":
    main()
