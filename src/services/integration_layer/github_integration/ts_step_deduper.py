import base64
import json
import logging
import re
from typing import List, Set, Tuple, Optional

import requests

logger = logging.getLogger(__name__)


class GitHubRepoReader:
    """
    Minimal GitHub reader to list and read files from a repo at a specific branch.
    """

    def __init__(self, token: str, owner: str, repo: str, branch: str = "main"):
        self.token = token
        self.owner = owner
        self.repo = repo
        self.branch = branch
        self.base_url = "https://api.github.com"
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "TS-Deduper/1.0",
        }

    def _get(self, url: str, params: Optional[dict] = None) -> Optional[requests.Response]:
        try:
            resp = requests.get(url, headers=self.headers, params=params, timeout=30)
            if resp.status_code == 200:
                return resp
            logger.warning(f"GitHub GET failed {resp.status_code}: {url} {resp.text[:200]}")
            return None
        except Exception as e:
            logger.warning(f"GitHub GET exception for {url}: {e}")
            return None

    def _get_branch_commit_sha(self) -> Optional[str]:
        url = f"{self.base_url}/repos/{self.owner}/{self.repo}/git/refs/heads/{self.branch}"
        resp = self._get(url)
        if not resp:
            return None
        try:
            data = resp.json()
            return (data.get("object") or {}).get("sha")
        except Exception:
            return None

    def _get_tree_sha_for_commit(self, commit_sha: str) -> Optional[str]:
        url = f"{self.base_url}/repos/{self.owner}/{self.repo}/git/commits/{commit_sha}"
        resp = self._get(url)
        if not resp:
            return None
        try:
            data = resp.json()
            tree = data.get("tree") or {}
            return tree.get("sha")
        except Exception:
            return None

    def list_files_recursive(self, prefix: str) -> List[str]:
        """
        List file paths under 'prefix' at the current branch using the Git Trees API (recursive).
        """
        commit_sha = self._get_branch_commit_sha()
        if not commit_sha:
            return []
        tree_sha = self._get_tree_sha_for_commit(commit_sha)
        if not tree_sha:
            return []
        url = f"{self.base_url}/repos/{self.owner}/{self.repo}/git/trees/{tree_sha}"
        resp = self._get(url, params={"recursive": "1"})
        if not resp:
            return []
        try:
            data = resp.json()
            paths = []
            for entry in data.get("tree", []):
                if entry.get("type") == "blob":
                    path = entry.get("path", "")
                    if path.startswith(prefix):
                        paths.append(path)
            return paths
        except Exception:
            return []

    def read_file(self, path: str) -> Optional[str]:
        """
        Read a file's text content using the Contents API at the current branch.
        """
        url = f"{self.base_url}/repos/{self.owner}/{self.repo}/contents/{path}"
        resp = self._get(url, params={"ref": self.branch})
        if not resp:
            return None
        try:
            data = resp.json()
            if isinstance(data, dict) and data.get("encoding") == "base64":
                content_b64 = data.get("content", "")
                return base64.b64decode(content_b64).decode("utf-8", errors="replace")
            # If it's not encoded (rare), try 'content' directly or fall back
            if isinstance(data, dict) and "content" in data and isinstance(data["content"], str):
                return data["content"]
        except Exception:
            pass
        return None


_STEP_START_RE = re.compile(
    r'^\s*(Given|When|Then)\s*\(\s*([`\'"])(.*?)\2\s*,', re.DOTALL
)


def _normalize_pattern(step_text: str) -> str:
    # Minimal normalization: trim and collapse multi-space to single space
    return re.sub(r"\s+", " ", (step_text or "").strip())


def _extract_repo_step_patterns(ts_text: str) -> Set[str]:
    """
    Extract step patterns from a TypeScript step file content.
    """
    existing: Set[str] = set()
    for line in ts_text.splitlines():
        m = _STEP_START_RE.match(line)
        if m:
            step_text = m.group(3)
            existing.add(_normalize_pattern(step_text))
    return existing


def _find_step_blocks(ts_text: str) -> List[Tuple[Tuple[int, int], str]]:
    """
    Identify step definition blocks with their line spans and extracted pattern.
    Returns list of ((start_idx, end_idx_inclusive), pattern).
    """
    lines = ts_text.splitlines()
    blocks: List[Tuple[Tuple[int, int], str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _STEP_START_RE.match(line)
        if not m:
            i += 1
            continue
        pattern = _normalize_pattern(m.group(3))
        # Find the opening brace of the step function block
        brace_depth = 0
        found_open = False
        start_idx = i
        j = i
        # Scan from current line to find first '{' that starts the function body
        while j < len(lines):
            brace_depth += lines[j].count('{')
            brace_depth -= lines[j].count('}')
            if not found_open and '{' in lines[j]:
                found_open = True
            if found_open and brace_depth <= 0:
                # We closed the function body '}' — include closing '});' if on next lines
                end_idx = j
                k = j
                # Extend through trailing ');' if present on subsequent line(s)
                while k + 1 < len(lines) and re.search(r'\)\s*;\s*$', lines[k + 1]):
                    k += 1
                    end_idx = k
                blocks.append(((start_idx, end_idx), pattern))
                break
            j += 1
        # Move to next line after the block we just processed to avoid nested matches
        i = (j + 1) if j > i else (i + 1)
    return blocks


def filter_ts_steps_against_repo(
    github_token: str,
    repo_owner: str,
    repo_name: str,
    base_branch: str,
    new_ts_content: str,
    steps_root: str = "src/tests/steps/",
) -> str:
    """
    Remove step definition functions from new_ts_content if their step pattern
    already exists in any .ts file under steps_root in the repository.
    """
    try:
        reader = GitHubRepoReader(github_token, repo_owner, repo_name, base_branch)
        paths = reader.list_files_recursive(steps_root)
        ts_paths = [p for p in paths if p.endswith(".ts")]
        logger.info(f"Deduper: scanning {len(ts_paths)} .ts files under {steps_root} in {repo_owner}/{repo_name}@{base_branch}")

        repo_patterns: Set[str] = set()
        for p in ts_paths:
            content = reader.read_file(p)
            if not content:
                continue
            repo_patterns |= _extract_repo_step_patterns(content)

        if not repo_patterns:
            logger.info("Deduper: no existing repo step patterns found; skipping filtering")
            return new_ts_content

        # Parse new content and identify blocks to remove
        blocks = _find_step_blocks(new_ts_content)
        if not blocks:
            logger.info("Deduper: no step blocks found in new TS content")
            return new_ts_content

        lines = new_ts_content.splitlines()
        remove_spans: List[Tuple[int, int]] = []
        removed_count = 0
        for (start, end), pattern in blocks:
            if pattern in repo_patterns:
                remove_spans.append((start, end))
                removed_count += 1

        if not remove_spans:
            logger.info("Deduper: no duplicates against repo found in new TS content")
            return new_ts_content

        # Merge overlapping spans and rebuild content
        remove_spans.sort()
        merged: List[Tuple[int, int]] = []
        for s, e in remove_spans:
            if not merged or s > merged[-1][1] + 1:
                merged.append((s, e))
            else:
                prev_s, prev_e = merged[-1]
                merged[-1] = (prev_s, max(prev_e, e))

        filtered_lines: List[str] = []
        idx = 0
        span_idx = 0
        while idx < len(lines):
            if span_idx < len(merged) and merged[span_idx][0] <= idx <= merged[span_idx][1]:
                # Skip this line (inside a removal span)
                idx = merged[span_idx][1] + 1
                span_idx += 1
                continue
            filtered_lines.append(lines[idx])
            idx += 1

        result = "\n".join(filtered_lines).strip("\n")
        logger.info(f"Deduper: removed {removed_count} duplicate step function(s) found in repo")
        # Post-process whitespace: trim trailing spaces, collapse multiple blank lines,
        # and ensure a single blank line after import block.
        return _tidy_typescript(result)
    except Exception as e:
        logger.warning(f"Deduper: error during filtering (will return original content): {e}")
        return new_ts_content


def _tidy_typescript(text: str) -> str:
    """
    Clean up formatting after deletions:
    - Trim trailing whitespace
    - Collapse multiple consecutive blank lines to a single blank line
    - Ensure exactly one blank line after the import block (if any content follows)
    - Remove leading/trailing blank lines
    """
    try:
        lines = text.splitlines()
        # Trim trailing whitespace
        lines = [ln.rstrip() for ln in lines]
        # Collapse multiple blank lines
        collapsed = []
        blank_run = 0
        for ln in lines:
            if ln.strip() == "":
                if blank_run == 0:
                    collapsed.append("")
                blank_run += 1
            else:
                collapsed.append(ln)
                blank_run = 0
        # Strip leading/trailing blanks
        while collapsed and collapsed[0] == "":
            collapsed.pop(0)
        while collapsed and collapsed[-1] == "":
            collapsed.pop()
        # Ensure a single blank line after the last import line if there is subsequent content
        last_import_idx = -1
        for idx, ln in enumerate(collapsed):
            if ln.lstrip().startswith("import "):
                last_import_idx = idx
        if last_import_idx >= 0 and last_import_idx + 1 < len(collapsed):
            # Count existing blank lines after imports
            pos = last_import_idx + 1
            while pos < len(collapsed) and collapsed[pos].strip() == "":
                pos += 1
            if pos < len(collapsed):
                # Rebuild with exactly one blank line after imports
                collapsed = collapsed[: last_import_idx + 1] + [""] + collapsed[pos:]
            else:
                # Nothing after imports; no need to enforce blank line
                collapsed = collapsed[: last_import_idx + 1]
        return "\n".join(collapsed)
    except Exception:
        # On any cleanup error, return original
        return text


