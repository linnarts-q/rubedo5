"""PR-flow tool (stage 6) — the model-facing wrapper over
agent/prflow.py. Defaults repo_path/repo_full_name to config's
REPO_PATH/REPO_FULL_NAME (Rubedo's own codebase) when the caller
doesn't name a different local repo explicitly.
"""
from __future__ import annotations


def propose_code_change(
    files: dict,
    branch_name: str,
    commit_message: str,
    pr_title: str,
    pr_body: str = "",
    repo_path: str = "",
    repo_full_name: str = "",
) -> str:
    from config import REPO_PATH, REPO_FULL_NAME
    from agent.prflow import propose_code_change as _propose

    path = repo_path or REPO_PATH
    full_name = repo_full_name or REPO_FULL_NAME
    if not path or not full_name:
        return (
            "REPO_PATH/REPO_FULL_NAME не настроены в .env — не знаю, "
            "какой репозиторий и где менять."
        )
    return _propose(
        repo_path=path, repo_full_name=full_name, files=files,
        branch_name=branch_name, commit_message=commit_message,
        pr_title=pr_title, pr_body=pr_body,
    )
