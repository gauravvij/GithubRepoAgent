"""
scanner.py - Codebase directory scanner module.

Recursively traverses a project directory, builds a hierarchical tree string,
and aggregates readable code file contents while ignoring irrelevant folders
and binary files.

Includes token estimation heuristics (1 token ≈ 4 characters) and algorithms
for content truncation, filtering, or chunking for files that exceed a
configurable safe threshold.
"""

import os
import logging
import sys
from typing import List, Dict, Optional

logging.basicConfig(
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
    format="%(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Directories to skip during traversal
IGNORED_DIRS = {
    ".git", ".svn", ".hg",
    "node_modules", "__pycache__", ".pytest_cache",
    "venv", ".venv", "env", ".env",
    "dist", "build", ".next", ".nuxt",
    ".idea", ".vscode", ".DS_Store",
    "coverage", ".nyc_output",
}

# File extensions considered as text/code files
TEXT_EXTENSIONS = {
    # Python
    ".py", ".pyw",
    # JavaScript / TypeScript
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    # Web
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    # Config / Data
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".env.example", ".env.sample",
    # Documentation
    ".md", ".rst", ".txt",
    # Shell
    ".sh", ".bash", ".zsh", ".fish",
    # Java / JVM
    ".java", ".kt", ".scala", ".groovy",
    # C / C++
    ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp",
    # Go
    ".go",
    # Rust
    ".rs",
    # Ruby
    ".rb",
    # PHP
    ".php",
    # Swift
    ".swift",
    # Dart
    ".dart",
    # SQL
    ".sql",
    # XML
    ".xml", ".xsl", ".xslt",
    # Other
    ".r", ".R", ".m", ".lua", ".pl", ".pm",
    ".dockerfile", ".Dockerfile",
    ".makefile", ".Makefile",
    ".gitignore", ".dockerignore",
    ".editorconfig",
}

# Maximum file size to read (1 MB default) for memory efficiency
MAX_FILE_SIZE_BYTES = 1 * 1024 * 1024  # 1 MB

# ─── Token estimation constants ───────────────────────────────────────────────
# Heuristic: 1 token ≈ 3 characters (conservative approximation for code/LLMs).
# Code files tend to have shorter tokens than prose — using 3 chars/token
# prevents chunk sizes from exceeding the API's hard 128k context limit.
# The previous value of 4 caused underestimation: chunks sized for ~95k tokens
# at 4 chars/token actually contained ~127k+ tokens at the real code ratio,
# triggering 400 "context length exceeded" errors from the API.
CHARS_PER_TOKEN: int = 3

# Default safe context window (tokens).  Gemini 2.5 Flash Lite supports up to
# ~1 000 000 tokens, but we leave a generous safety margin for the system
# prompt, the analysis prompt template, and the model's own output budget.
# 800 000 tokens × 4 chars ≈ 3.2 MB of raw text — a very large codebase.
DEFAULT_SAFE_TOKEN_LIMIT: int = 800_000

# Per-file truncation threshold: files larger than this many tokens will be
# truncated to this limit with a notice appended.
DEFAULT_FILE_TOKEN_LIMIT: int = 4_000  # ≈ 16 000 chars per file


def estimate_tokens(text: str) -> int:
    """
    Estimate the number of LLM tokens in a string using the heuristic
    1 token ≈ 4 characters.

    Args:
        text: The text whose token count should be estimated.

    Returns:
        Estimated token count (integer).
    """
    return max(0, len(text) // CHARS_PER_TOKEN)


def truncate_to_token_limit(text: str, max_tokens: int, notice: str = "") -> str:
    """
    Truncate *text* so that its estimated token count does not exceed
    *max_tokens*.  An optional *notice* string is appended after truncation
    to inform the reader that content was cut.

    Args:
        text:       The original text.
        max_tokens: Maximum allowed estimated token count.
        notice:     Message to append when truncation occurs.

    Returns:
        The (possibly truncated) text string.
    """
    max_chars = max_tokens * CHARS_PER_TOKEN
    if len(text) <= max_chars:
        return text

    truncated = text[:max_chars]
    if notice:
        truncated += f"\n{notice}"
    return truncated


def chunk_text(text: str, chunk_token_size: int) -> List[str]:
    """
    Split *text* into a list of chunks where each chunk's estimated token
    count does not exceed *chunk_token_size*.

    Splitting is done on newline boundaries where possible to avoid breaking
    mid-line.

    Args:
        text:             The text to split.
        chunk_token_size: Maximum tokens per chunk.

    Returns:
        List of text chunks.
    """
    max_chars = chunk_token_size * CHARS_PER_TOKEN
    if len(text) <= max_chars:
        return [text]

    chunks: List[str] = []
    lines = text.splitlines(keepends=True)
    current_chunk: List[str] = []
    current_len = 0

    for line in lines:
        line_len = len(line)
        # If a single line is longer than the chunk limit, hard-split it
        if line_len > max_chars:
            # Flush current chunk first
            if current_chunk:
                chunks.append("".join(current_chunk))
                current_chunk = []
                current_len = 0
            # Hard-split the oversized line
            for start in range(0, line_len, max_chars):
                chunks.append(line[start: start + max_chars])
            continue

        if current_len + line_len > max_chars:
            chunks.append("".join(current_chunk))
            current_chunk = [line]
            current_len = line_len
        else:
            current_chunk.append(line)
            current_len += line_len

    if current_chunk:
        chunks.append("".join(current_chunk))

    return chunks


def _is_text_file(filepath: str) -> bool:
    """
    Determine if a file is a text/code file based on its extension.

    Args:
        filepath: Absolute or relative path to the file.

    Returns:
        True if the file has a recognized text extension, False otherwise.
    """
    _, ext = os.path.splitext(filepath)
    # Handle files like Dockerfile, Makefile (no extension)
    basename = os.path.basename(filepath)
    if basename.lower() in {"dockerfile", "makefile", "gemfile", "rakefile", "procfile"}:
        return True
    return ext.lower() in TEXT_EXTENSIONS


def _build_tree(
    root: str,
    prefix: str = "",
    is_last: bool = True,
    lines: list = None,
) -> list:
    """
    Recursively build a list of strings representing the directory tree.

    Args:
        root:    Current directory path being processed.
        prefix:  Visual prefix string for indentation.
        is_last: Whether this entry is the last child in its parent.
        lines:   Accumulator list for tree lines.

    Returns:
        List of strings forming the tree representation.
    """
    if lines is None:
        lines = []

    connector = "└── " if is_last else "├── "
    lines.append(prefix + connector + os.path.basename(root))

    if os.path.isdir(root):
        extension = "    " if is_last else "│   "
        try:
            entries = sorted(os.listdir(root))
        except PermissionError:
            lines.append(prefix + extension + "└── [Permission Denied]")
            return lines

        # Filter out ignored directories
        filtered = []
        for entry in entries:
            full_path = os.path.join(root, entry)
            if os.path.isdir(full_path) and entry in IGNORED_DIRS:
                continue
            filtered.append(entry)

        for i, entry in enumerate(filtered):
            full_path = os.path.join(root, entry)
            _build_tree(
                full_path,
                prefix=prefix + extension,
                is_last=(i == len(filtered) - 1),
                lines=lines,
            )

    return lines


def build_directory_tree(project_dir: str) -> str:
    """
    Build a hierarchical directory tree string for the given project directory.

    Args:
        project_dir: Path to the root of the project directory.

    Returns:
        A formatted string representing the directory hierarchy.
    """
    if not os.path.isdir(project_dir):
        raise ValueError(f"Provided path is not a valid directory: {project_dir}")

    lines = [os.path.abspath(project_dir)]
    try:
        entries = sorted(os.listdir(project_dir))
    except PermissionError:
        return lines[0] + "\n└── [Permission Denied]"

    filtered = [
        e for e in entries
        if not (os.path.isdir(os.path.join(project_dir, e)) and e in IGNORED_DIRS)
    ]

    for i, entry in enumerate(filtered):
        full_path = os.path.join(project_dir, entry)
        _build_tree(
            full_path,
            prefix="",
            is_last=(i == len(filtered) - 1),
            lines=lines,
        )

    return "\n".join(lines)


def collect_code_files(
    project_dir: str,
    file_token_limit: int = DEFAULT_FILE_TOKEN_LIMIT,
) -> str:
    """
    Recursively collect all readable text/code files and return their contents
    as a single composite string with clear delimiters.

    Files whose estimated token count exceeds *file_token_limit* are truncated
    with a notice so that no single file dominates the context window.

    Args:
        project_dir:      Path to the root of the project directory.
        file_token_limit: Maximum tokens to include per file before truncating.

    Returns:
        A composite string with each file's content preceded by a delimiter
        header.  Files exceeding MAX_FILE_SIZE_BYTES are noted but not fully
        included.
    """
    if not os.path.isdir(project_dir):
        raise ValueError(f"Provided path is not a valid directory: {project_dir}")

    parts = []
    abs_root = os.path.abspath(project_dir)

    for dirpath, dirnames, filenames in os.walk(abs_root):
        # Prune ignored directories in-place to prevent os.walk from descending
        dirnames[:] = sorted(
            d for d in dirnames if d not in IGNORED_DIRS
        )

        for filename in sorted(filenames):
            filepath = os.path.join(dirpath, filename)
            relative_path = os.path.relpath(filepath, abs_root)

            if not _is_text_file(filepath):
                logger.debug("Skipping binary/non-text file: %s", relative_path)
                continue

            try:
                file_size = os.path.getsize(filepath)
            except OSError as e:
                logger.warning("Cannot stat file %s: %s", relative_path, e)
                continue

            if file_size > MAX_FILE_SIZE_BYTES:
                parts.append(
                    f"--- {relative_path} ---\n"
                    f"[File too large to include: {file_size / 1024:.1f} KB, "
                    f"threshold is {MAX_FILE_SIZE_BYTES // 1024} KB]\n"
                )
                logger.info("Skipping large file: %s (%d bytes)", relative_path, file_size)
                continue

            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except OSError as e:
                logger.warning("Cannot read file %s: %s", relative_path, e)
                parts.append(f"--- {relative_path} ---\n[Read error: {e}]\n")
                continue

            # ── Per-file token truncation ──────────────────────────────────
            estimated = estimate_tokens(content)
            if estimated > file_token_limit:
                notice = (
                    f"\n[... TRUNCATED: file estimated at ~{estimated} tokens; "
                    f"showing first ~{file_token_limit} tokens ({file_token_limit * CHARS_PER_TOKEN} chars). "
                    f"Full file available on disk at {relative_path} ...]"
                )
                content = truncate_to_token_limit(content, file_token_limit, notice)
                logger.info(
                    "Truncated large file %s (~%d tokens → %d token limit)",
                    relative_path, estimated, file_token_limit,
                )

            parts.append(f"--- {relative_path} ---\n{content}\n")

    if not parts:
        return "[No readable code files found in the directory]"

    return "\n".join(parts)


def collect_code_files_chunked(
    project_dir: str,
    chunk_token_size: int,
    file_token_limit: int = DEFAULT_FILE_TOKEN_LIMIT,
) -> List[str]:
    """
    Collect all code file contents and split the result into chunks that each
    fit within *chunk_token_size* tokens.

    This is the primary entry point for large-codebase processing where the
    full aggregated content would exceed the LLM's safe context window.

    Args:
        project_dir:      Path to the root of the project directory.
        chunk_token_size: Maximum tokens per returned chunk.
        file_token_limit: Maximum tokens to include per individual file.

    Returns:
        List of text chunks, each within the token budget.
    """
    full_content = collect_code_files(project_dir, file_token_limit=file_token_limit)
    return chunk_text(full_content, chunk_token_size)


def scan_project(
    project_dir: str,
    safe_token_limit: int = DEFAULT_SAFE_TOKEN_LIMIT,
    file_token_limit: int = DEFAULT_FILE_TOKEN_LIMIT,
) -> dict:
    """
    Main entry point for scanning a project directory.

    Produces both a directory tree string and an aggregated code content
    string.  Also reports token estimates and whether the content fits within
    the safe context window.

    Args:
        project_dir:      Path to the root of the project directory.
        safe_token_limit: Total token budget for the aggregated code content.
                          If the content exceeds this, the caller should use
                          chunked processing via collect_code_files_chunked().
        file_token_limit: Per-file token cap before truncation.

    Returns:
        A dictionary with keys:
        - 'directory_tree'   (str):  Hierarchical tree of the project.
        - 'code_contents'    (str):  Aggregated code file contents.
        - 'project_path'     (str):  Absolute path of the scanned project.
        - 'estimated_tokens' (int):  Estimated token count of code_contents.
        - 'exceeds_limit'    (bool): True if content exceeds safe_token_limit.
        - 'safe_token_limit' (int):  The limit that was applied.
    """
    abs_path = os.path.abspath(project_dir)
    logger.info("Scanning project directory: %s", abs_path)

    directory_tree = build_directory_tree(abs_path)
    code_contents = collect_code_files(abs_path, file_token_limit=file_token_limit)

    estimated_tokens = estimate_tokens(code_contents)
    exceeds_limit = estimated_tokens > safe_token_limit

    if exceeds_limit:
        logger.warning(
            "Codebase estimated at ~%d tokens, which exceeds the safe limit of %d tokens. "
            "The agent will use chunked/Map-Reduce processing.",
            estimated_tokens, safe_token_limit,
        )
    else:
        logger.info(
            "Codebase estimated at ~%d tokens (within safe limit of %d tokens).",
            estimated_tokens, safe_token_limit,
        )

    logger.info("Scan complete.")
    return {
        "project_path": abs_path,
        "directory_tree": directory_tree,
        "code_contents": code_contents,
        "estimated_tokens": estimated_tokens,
        "exceeds_limit": exceeds_limit,
        "safe_token_limit": safe_token_limit,
    }


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    result = scan_project(target)
    print("=== DIRECTORY TREE ===")
    print(result["directory_tree"])
    print(f"\n=== TOKEN ESTIMATE ===")
    print(f"Estimated tokens : {result['estimated_tokens']:,}")
    print(f"Safe token limit : {result['safe_token_limit']:,}")
    print(f"Exceeds limit    : {result['exceeds_limit']}")
    print("\n=== CODE CONTENTS (first 3000 chars) ===")
    print(result["code_contents"][:3000], "..." if len(result["code_contents"]) > 3000 else "")
