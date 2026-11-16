"""Guard the CI/CD supply-chain rules so they cannot quietly regress."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
PINNED = re.compile(r"^[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}$")


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_workflows_exist():
    assert {p.name for p in WORKFLOWS} == {"ci.yml", "pages.yml"}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_hardening(path):
    text = path.read_text(encoding="utf-8")
    workflow = load(path)
    triggers = workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` as True

    assert workflow["permissions"] == {}
    assert "pull_request_target" not in triggers
    assert "secrets." not in text
    assert "concurrency" in workflow
    for name, job in workflow["jobs"].items():
        assert "permissions" in job, f"{name} must declare its own permissions"
        assert job.get("timeout-minutes"), f"{name} needs timeout-minutes"
        for step in job.get("steps", []):
            if "uses" in step:
                assert PINNED.match(step["uses"]), f"{step['uses']} is not pinned to a SHA"
            if "actions/checkout" in step.get("uses", ""):
                assert step["with"]["persist-credentials"] is False


def test_job_permissions_are_least_privilege():
    ci, pages = (load(ROOT / ".github" / "workflows" / name) for name in ("ci.yml", "pages.yml"))
    assert all(job["permissions"] == {"contents": "read"} for job in ci["jobs"].values())
    assert pages["jobs"]["build"]["permissions"] == {"contents": "read"}
    assert pages["jobs"]["deploy"]["permissions"] == {"pages": "write", "id-token": "write"}


def test_scheduled_publish_never_runs_on_forks():
    pages = load(ROOT / ".github" / "workflows" / "pages.yml")
    assert pages["jobs"]["build"]["if"] == "github.repository == 'hponukumati/quakewatch'"
    assert pages["jobs"]["deploy"]["needs"] == "build"


def test_installs_require_hashes():
    for path in WORKFLOWS:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "pip install" in line:
                assert "--require-hashes" in line, line
