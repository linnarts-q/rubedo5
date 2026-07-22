"""PR-flow (stage 6): a code change to a git-tracked project goes
through a branch + commit + pull request, not a direct file write
landing straight in the running tree — the gap this closes is real:
`file_write` on a path outside workspace/ is already yellow-zone
(approval-gated, §1) and undo-snapshotted (§15), but it's still a
live edit to Rubedo's own source (or another tracked project) the
instant the owner says "да". A branch + PR gives a human an actual
diff to review before anything lands, the same "propose, then a human
decides" spirit as every other yellow/red-zone mechanism here — it
doesn't merge or deploy anything itself.

Scope: local git repos only (the checkout this process runs from, or
another local repo path). A remote project like SpotRent (server-side,
reached only via SSH — agent/remote.py) would need git-over-SSH
plumbing this doesn't build yet; propose_code_change take repo_path/
repo_full_name explicitly rather than hard-coding "self" so that
extension has somewhere to plug in later.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.error
import urllib.request

from config import GITHUB_TOKEN, GITHUB_API_BASE_URL

log = logging.getLogger("rubedo.agent.prflow")


def _run_git(args: list[str], cwd: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _create_pull_request(
    repo_full_name: str, head: str, base: str, title: str, body: str,
) -> str:
    """Returns the PR's html_url, or a message describing what went
    wrong (never raises — the branch is already pushed at this point,
    so a failure here still needs to tell the owner where to look)."""
    url = f"{GITHUB_API_BASE_URL}/repos/{repo_full_name}/pulls"
    payload = json.dumps({
        "title": title, "head": head, "base": base, "body": body, "draft": True,
    }).encode()
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data.get("html_url") or "PR создан, но ссылку получить не удалось."
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        log.error(f"GitHub PR creation failed: {e.code} {detail}")
        return f"Ветка {head} запушена, но PR создать не удалось ({e.code}): {detail[:200]}"
    except Exception as e:
        log.error(f"GitHub PR creation failed: {e}")
        return f"Ветка {head} запушена, но PR создать не удалось: {e}"


def propose_code_change(
    repo_path: str,
    repo_full_name: str,
    files: dict[str, str],
    branch_name: str,
    commit_message: str,
    pr_title: str,
    pr_body: str = "",
    base_branch: str = "main",
) -> str:
    """Create `branch_name` off `base_branch`, write `files`
    (repo-relative path -> new full content), commit, push, and open a
    draft PR. Returns the PR URL on success, or a plain-language error
    — this never raises past its own boundary, since a failed proposal
    is a normal, expected outcome (bad token, network hiccup, nothing
    to commit) that the caller just needs to relay, not crash on."""
    if not GITHUB_TOKEN:
        return "GITHUB_TOKEN не настроен — не могу открыть PR."
    if not repo_path or not os.path.isdir(repo_path):
        return f"Репозиторий не найден по пути: {repo_path!r}"
    if not files:
        return "Нет файлов для изменения."

    try:
        _run_git(["fetch", "origin", base_branch], repo_path)
        _run_git(["checkout", "-B", branch_name, f"origin/{base_branch}"], repo_path)
        for rel_path, content in files.items():
            full = os.path.join(repo_path, rel_path)
            os.makedirs(os.path.dirname(full) or repo_path, exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)
            _run_git(["add", rel_path], repo_path)
        _run_git(["commit", "-m", commit_message], repo_path)
        _run_git(["push", "-u", "origin", branch_name], repo_path)
    except RuntimeError as e:
        log.error(f"PR-flow git step failed: {e}")
        return f"Не получилось подготовить изменение: {e}"

    return _create_pull_request(repo_full_name, branch_name, base_branch, pr_title, pr_body)
