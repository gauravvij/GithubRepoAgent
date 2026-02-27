"""
github_utils.py - GitHub repository downloader utility.

Provides a reliable function to clone or download and extract a user-provided
GitHub repository URL into a local /tmp directory. Supports both git clone
(when git is available) and ZIP archive download as a fallback.
"""

import os
import re
import shutil
import logging
import sys
import subprocess
import tempfile
import zipfile
from urllib.parse import urlparse

import requests

logging.basicConfig(
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
    format="%(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Base directory for cloned repositories
DEFAULT_CLONE_BASE = os.environ.get("GITHUB_CLONE_BASE", "/tmp/codebase_agent_repos")

# Custom exception hierarchy for granular error reporting
class GitHubRepoError(RuntimeError):
    """Base class for GitHub repository access errors."""

class RepoNotFoundError(GitHubRepoError):
    """Raised when the repository does not exist (HTTP 404)."""

class RepoPrivateError(GitHubRepoError):
    """Raised when the repository is private or access is forbidden (HTTP 403/401)."""

class RepoCloneError(GitHubRepoError):
    """Raised when cloning fails for a generic/network reason."""


def _normalize_github_url(url: str) -> str:
    """
    Normalize a GitHub URL to a canonical HTTPS form without trailing slashes
    or .git suffix.

    Args:
        url: Raw GitHub URL provided by the user.

    Returns:
        Normalized HTTPS GitHub URL string.

    Raises:
        ValueError: If the URL does not appear to be a valid GitHub repository URL.
    """
    url = url.strip().rstrip("/")

    # Accept SSH format: git@github.com:owner/repo.git
    ssh_match = re.match(r"^git@github\.com:([^/]+)/(.+?)(?:\.git)?$", url)
    if ssh_match:
        owner, repo = ssh_match.group(1), ssh_match.group(2)
        return f"https://github.com/{owner}/{repo}"

    # Accept HTTPS format
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http", ""):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme}")

    # Ensure it's a github.com URL
    host = parsed.netloc or parsed.path.split("/")[0]
    if "github.com" not in host:
        raise ValueError(f"URL does not appear to be a GitHub URL: {url}")

    # Strip .git suffix from path
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]

    # Validate owner/repo structure
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise ValueError(
            f"GitHub URL must include owner and repository name: {url}"
        )

    owner, repo = parts[0], parts[1]
    return f"https://github.com/{owner}/{repo}"


def _repo_slug(normalized_url: str) -> str:
    """
    Extract a filesystem-safe slug from a normalized GitHub URL.

    Args:
        normalized_url: Normalized GitHub HTTPS URL.

    Returns:
        A string like 'owner__repo' suitable for use as a directory name.
    """
    parts = normalized_url.rstrip("/").split("/")
    owner, repo = parts[-2], parts[-1]
    return f"{owner}__{repo}"


def _check_repo_accessibility(repo_url: str) -> None:
    """
    Probe the GitHub repository URL via HTTP HEAD to detect access issues
    before attempting a full clone.

    Args:
        repo_url: Normalized HTTPS GitHub repository URL.

    Raises:
        RepoNotFoundError: If the repository returns HTTP 404.
        RepoPrivateError: If the repository returns HTTP 401 or 403.
        RepoCloneError: For other non-200 HTTP responses.
    """
    try:
        resp = requests.head(repo_url, timeout=15, allow_redirects=True)
        status = resp.status_code
        if status == 404:
            raise RepoNotFoundError(
                f"Repository not found (404): '{repo_url}'. "
                "Please verify the URL is correct and the repository exists."
            )
        if status in (401, 403):
            raise RepoPrivateError(
                f"Access denied ({status}): '{repo_url}'. "
                "This repository is private or requires authentication. "
                "Only public repositories can be analyzed."
            )
        if status >= 400:
            raise RepoCloneError(
                f"GitHub returned HTTP {status} for '{repo_url}'. "
                "The repository may be unavailable or the URL is incorrect."
            )
    except (RepoNotFoundError, RepoPrivateError, RepoCloneError):
        raise
    except requests.RequestException as e:
        logger.warning("Accessibility pre-check failed (network): %s", e)
        # Don't block on network errors during pre-check; let clone attempt proceed


def _parse_git_error(stderr: str, repo_url: str) -> GitHubRepoError:
    """
    Parse git clone stderr output and return a descriptive exception.

    Args:
        stderr: The stderr string from the failed git clone command.
        repo_url: The repository URL that was being cloned.

    Returns:
        An appropriate GitHubRepoError subclass with a human-readable message.
    """
    stderr_lower = stderr.lower()

    if "repository not found" in stderr_lower or "not found" in stderr_lower:
        return RepoNotFoundError(
            f"Repository not found: '{repo_url}'. "
            "Please verify the URL is correct and the repository exists."
        )
    if (
        "could not read username" in stderr_lower
        or "authentication failed" in stderr_lower
        or "access denied" in stderr_lower
        or "permission denied" in stderr_lower
        or "403" in stderr_lower
        or "401" in stderr_lower
    ):
        return RepoPrivateError(
            f"Access denied to '{repo_url}'. "
            "This repository is private or requires authentication. "
            "Only public repositories can be analyzed."
        )
    if "timed out" in stderr_lower or "timeout" in stderr_lower:
        return RepoCloneError(
            f"Clone timed out for '{repo_url}'. "
            "The repository may be too large or the network is slow. Please try again."
        )
    if "remote: repository" in stderr_lower and "blocked" in stderr_lower:
        return RepoPrivateError(
            f"Repository '{repo_url}' is blocked or restricted by GitHub."
        )

    return RepoCloneError(
        f"Git clone failed for '{repo_url}': {stderr.strip()[:300]}"
    )


def _clone_with_git(repo_url: str, dest_dir: str) -> bool:
    """
    Attempt to clone a repository using the system git command.

    Args:
        repo_url: HTTPS GitHub repository URL.
        dest_dir: Destination directory path (must not already exist).

    Returns:
        True if clone succeeded, False otherwise.

    Raises:
        RepoNotFoundError: If git reports the repository does not exist.
        RepoPrivateError: If git reports authentication/permission failure.
        RepoCloneError: For other git-reported failures.
    """
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, dest_dir],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            logger.info("Git clone succeeded: %s -> %s", repo_url, dest_dir)
            return True
        else:
            stderr = result.stderr.strip()
            logger.warning("Git clone failed (exit %d): %s", result.returncode, stderr)
            # Raise specific error based on stderr content
            raise _parse_git_error(stderr, repo_url)
    except FileNotFoundError:
        logger.warning("git binary not found; falling back to ZIP download.")
        return False
    except subprocess.TimeoutExpired:
        raise RepoCloneError(
            f"Git clone timed out after 120 seconds for '{repo_url}'. "
            "The repository may be too large. Please try again."
        )
    except (RepoNotFoundError, RepoPrivateError, RepoCloneError):
        raise
    except Exception as e:
        logger.warning("Git clone unexpected error: %s", e)
        return False


def _download_zip(repo_url: str, dest_dir: str, branch: str = "HEAD") -> bool:
    """
    Download the repository as a ZIP archive from GitHub and extract it.

    Args:
        repo_url:  Normalized HTTPS GitHub repository URL.
        dest_dir:  Destination directory where contents will be extracted.
        branch:    Branch/ref to download (default 'HEAD' resolves to default branch).

    Returns:
        True if download and extraction succeeded, False otherwise.

    Raises:
        RepoNotFoundError: If GitHub returns 404 for the ZIP URL.
        RepoPrivateError: If GitHub returns 401/403 for the ZIP URL.
        RepoCloneError: For other HTTP errors during ZIP download.
    """
    zip_url = f"{repo_url}/archive/{branch}.zip"
    logger.info("Downloading ZIP archive: %s", zip_url)

    try:
        response = requests.get(zip_url, stream=True, timeout=60)
    except requests.RequestException as e:
        logger.error("Failed to download ZIP: %s", e)
        raise RepoCloneError(
            f"Network error while downloading '{repo_url}': {e}. "
            "Please check your connection and try again."
        )

    if response.status_code == 404:
        raise RepoNotFoundError(
            f"Repository not found (404): '{repo_url}'. "
            "Please verify the URL is correct and the repository exists."
        )
    if response.status_code in (401, 403):
        raise RepoPrivateError(
            f"Access denied ({response.status_code}): '{repo_url}'. "
            "This repository is private or requires authentication. "
            "Only public repositories can be analyzed."
        )
    if response.status_code >= 400:
        raise RepoCloneError(
            f"GitHub returned HTTP {response.status_code} for '{repo_url}'. "
            "The repository may be unavailable or the URL is incorrect."
        )

    # Write ZIP to a temp file
    try:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_file:
            tmp_path = tmp_file.name
            for chunk in response.iter_content(chunk_size=8192):
                tmp_file.write(chunk)
        logger.info("ZIP downloaded to: %s", tmp_path)
    except OSError as e:
        logger.error("Failed to write ZIP temp file: %s", e)
        raise RepoCloneError(f"Failed to save downloaded archive: {e}")

    # Extract ZIP
    try:
        os.makedirs(dest_dir, exist_ok=True)
        with zipfile.ZipFile(tmp_path, "r") as zf:
            # GitHub ZIPs have a top-level folder like 'repo-main/' or 'repo-HEAD/'
            members = zf.namelist()
            top_level = members[0].split("/")[0] if members else ""

            zf.extractall(dest_dir)

            # Flatten: move contents of top-level folder up one level
            extracted_top = os.path.join(dest_dir, top_level)
            if top_level and os.path.isdir(extracted_top):
                for item in os.listdir(extracted_top):
                    shutil.move(os.path.join(extracted_top, item), dest_dir)
                os.rmdir(extracted_top)

        logger.info("ZIP extracted to: %s", dest_dir)
        return True
    except (zipfile.BadZipFile, OSError) as e:
        logger.error("Failed to extract ZIP: %s", e)
        raise RepoCloneError(f"Failed to extract downloaded archive: {e}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def clone_github_repo(
    github_url: str,
    clone_base: str = DEFAULT_CLONE_BASE,
    force_redownload: bool = False,
) -> str:
    """
    Clone or download a GitHub repository to a local directory.

    Strategy:
        1. Normalize and validate the GitHub URL.
        2. If the destination directory already exists and force_redownload is False,
           return the cached path immediately.
        3. Probe the URL for accessibility (404/403 detection).
        4. Try git clone (shallow, depth=1) first.
        5. Fall back to ZIP archive download if git is unavailable.

    Args:
        github_url:       GitHub repository URL (HTTPS or SSH format).
        clone_base:       Base directory under which repos are stored.
        force_redownload: If True, delete existing clone and re-download.

    Returns:
        Absolute path to the local directory containing the repository.

    Raises:
        ValueError: If the URL is not a valid GitHub repository URL.
        RepoNotFoundError: If the repository does not exist (404).
        RepoPrivateError: If the repository is private or access is denied (401/403).
        RepoCloneError: If both git clone and ZIP download fail for other reasons.
    """
    normalized_url = _normalize_github_url(github_url)
    slug = _repo_slug(normalized_url)
    dest_dir = os.path.join(clone_base, slug)

    logger.info("Repository URL: %s", normalized_url)
    logger.info("Local destination: %s", dest_dir)

    # Return cached clone if it exists
    if os.path.isdir(dest_dir) and not force_redownload:
        logger.info("Using cached repository at: %s", dest_dir)
        return dest_dir

    # Remove stale clone if force_redownload
    if os.path.isdir(dest_dir) and force_redownload:
        logger.info("Removing stale clone: %s", dest_dir)
        shutil.rmtree(dest_dir)

    os.makedirs(clone_base, exist_ok=True)

    # Pre-check accessibility to give early, specific errors
    _check_repo_accessibility(normalized_url)

    # Attempt 1: git clone (raises specific errors on failure)
    try:
        if _clone_with_git(normalized_url, dest_dir):
            return dest_dir
    except (RepoNotFoundError, RepoPrivateError, RepoCloneError):
        # Re-raise specific errors immediately — no point trying ZIP
        raise

    # Attempt 2: ZIP download (git binary not available)
    _download_zip(normalized_url, dest_dir)
    return dest_dir


if __name__ == "__main__":
    """Quick smoke test: clone a small public repo."""
    test_url = sys.argv[1] if len(sys.argv) > 1 else "https://github.com/octocat/Hello-World"
    try:
        path = clone_github_repo(test_url, force_redownload=True)
        print(f"\nRepository available at: {path}")
        print("Contents:", os.listdir(path))
    except (ValueError, GitHubRepoError) as e:
        print(f"Error: {e}")
        sys.exit(1)
