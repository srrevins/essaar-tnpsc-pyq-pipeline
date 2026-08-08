#!/usr/bin/env python3
"""Deploy/restart the free Hugging Face Space that runs the PYQ worker."""

from __future__ import annotations

import os
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi


ROOT = Path(__file__).resolve().parent
DEFAULT_SPACE_REPO = "essaar/essaar-tnpsc-pyq-worker"

SPACE_FILES = {
    "space/README.md": "README.md",
    "space/Dockerfile": "Dockerfile",
    "space/app.py": "app.py",
    "pipeline.py": "pipeline.py",
    "requirements.txt": "requirements.txt",
    "config/syllabus_tags.json": "config/syllabus_tags.json",
    "data/manifest.json": "data/manifest.json",
    "dataset/README.md": "dataset/README.md",
}


def main() -> int:
    token = os.environ.get("HF_TOKEN", "").strip()
    dataset_repo = os.environ.get("HF_DATASET_REPO", "").strip()
    space_repo = os.environ.get("HF_SPACE_REPO", DEFAULT_SPACE_REPO).strip()
    process_limit = os.environ.get("PROCESS_LIMIT", "3").strip()
    if not token or not dataset_repo:
        raise SystemExit("HF_TOKEN and HF_DATASET_REPO are required")

    api = HfApi(token=token)
    api.create_repo(
        repo_id=space_repo,
        repo_type="space",
        space_sdk="docker",
        private=False,
        exist_ok=True,
    )
    operations = [
        CommitOperationAdd(path_in_repo=remote, path_or_fileobj=ROOT / local)
        for local, remote in SPACE_FILES.items()
    ]
    api.create_commit(
        repo_id=space_repo,
        repo_type="space",
        operations=operations,
        commit_message="Deploy zero-cost TNPSC PYQ worker",
    )
    api.add_space_secret(space_repo, "HF_TOKEN", token)
    api.add_space_variable(space_repo, "HF_DATASET_REPO", dataset_repo)
    api.add_space_variable(space_repo, "PROCESS_LIMIT", process_limit)
    runtime = api.restart_space(space_repo)
    print(f"Space restarted: {space_repo} ({runtime.stage})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
