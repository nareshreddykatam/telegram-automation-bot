"""Static validation of the Phase 3 deployment artifacts.

These tests never contact Vercel, GitHub, Supabase or Telegram. They check that
the prepared artifacts are internally consistent and that the worker template is
not silently activated.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOYMENT = ROOT / "deployment"
WORKFLOW_TEMPLATE = DEPLOYMENT / "worker.yml.template"

REQUIRED_DOCS = {
    "PHASE3_READINESS.md",
    "SUPABASE_SETUP.md",
    "VERCEL_SETUP.md",
    "GITHUB_ACTIONS_SETUP.md",
    "TELEGRAM_WEBHOOK_SETUP.md",
    "ROLLBACK.md",
}
REQUIRED_WORKFLOW_DOCS = {"worker.yml.template"}

PINNED_ACTION_RE = re.compile(r"uses:\s*[\w.-]+/[\w.-]+@[0-9a-f]{40}\s+#\s*v[\w.]+")


def test_vercel_config_caps_the_webhook_duration():
    config = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))

    function = config["functions"]["api/telegram.py"]
    assert function["maxDuration"] == 60


def test_vercel_entrypoint_exports_an_asgi_app_without_polling():
    source = (ROOT / "api" / "telegram.py").read_text(encoding="utf-8")

    assert "app = create_webhook_app(settings)" in source
    assert "start_polling" not in source
    assert "from app.main" not in source
    assert "import app.main" not in source
    assert "PublishingScheduler(" not in source


def test_worker_template_is_not_active():
    workflows = ROOT / ".github" / "workflows"
    if workflows.exists():
        names = {path.name for path in workflows.iterdir() if path.is_file()}
        assert "publishing-worker.yml" not in names
        assert "worker.yml" not in names


def test_worker_template_schedule_permissions_and_limits():
    text = WORKFLOW_TEMPLATE.read_text(encoding="utf-8")

    assert 'cron: "30 14 * * *"' in text
    assert "workflow_dispatch:" in text
    assert "timeout-minutes: 60" in text
    assert "permissions:\n  contents: read" in text
    assert "persist-credentials: false" in text
    assert "cancel-in-progress: false" in text
    # No automatic execution on pushes or pull requests.
    assert not re.search(r"^\s{2}(push|pull_request):", text, re.MULTILINE)
    # The only run command is the bounded worker entrypoint.
    assert "run: python -m app.worker" in text


def test_worker_template_pins_every_action_to_a_commit_sha():
    text = WORKFLOW_TEMPLATE.read_text(encoding="utf-8")
    uses_lines = [line for line in text.splitlines() if "uses:" in line]

    assert len(uses_lines) == 2
    for line in uses_lines:
        assert PINNED_ACTION_RE.search(line.strip()), line


def test_worker_template_documents_required_secrets_without_values():
    text = WORKFLOW_TEMPLATE.read_text(encoding="utf-8")

    assert "secrets.TELEGRAM_BOT_TOKEN" in text
    assert "secrets.DATABASE_URL" in text
    assert "vars.OWNER_IDS" in text
    # Runtime aliasing must be explicit: the worker reads BOT_TOKEN.
    assert "BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}" in text
    # No secret should be echoed or exported to the log.
    assert "printenv" not in text
    assert "set -x" not in text
    assert not re.search(r"echo\s+\$\{\{\s*secrets\.", text)


def test_production_env_template_contains_only_placeholders():
    text = (ROOT / ".env.production.example").read_text(encoding="utf-8")

    assignments = re.findall(r"^([A-Z0-9_]+)=(.*)$", text, re.MULTILINE)
    values = {name: value for name, value in assignments}

    assert values["DATABASE_BACKEND"] == "postgres"
    assert values["SCHEDULER_ENABLED"] == "false"
    assert values["DB_STATEMENT_CACHE_SIZE"] == "0"
    assert values["DB_POOL_MAX"] == "1"
    assert values["LOG_FILE"] == ""
    assert "REPLACE" in values["BOT_TOKEN"]
    assert "REPLACE" in values["DATABASE_URL"]
    assert "REPLACE" in values["TELEGRAM_WEBHOOK_SECRET"]
    # Nothing that looks like a live Telegram token or a real password.
    assert not re.search(r"^\s*[A-Z0-9_]*TOKEN=\d{6,}:[A-Za-z0-9_-]{20,}", text, re.MULTILINE)
    pooler_dsn = values["DATABASE_URL"]
    assert pooler_dsn.startswith("postgresql://REPLACE_")
    assert ":6543/postgres?sslmode=require" in pooler_dsn


def test_required_deployment_documentation_exists():
    present = {path.name for path in DEPLOYMENT.iterdir() if path.is_file()}

    assert REQUIRED_DOCS <= present
    assert REQUIRED_WORKFLOW_DOCS <= present


def test_readiness_document_covers_required_topics():
    text = (DEPLOYMENT / "PHASE3_READINESS.md").read_text(encoding="utf-8").lower()

    for topic in (
        "readiness",
        "blocker",
        "rollback",
        "cost",
        "end-to-end",
        "test",
        "supabase",
        "vercel",
        "github actions",
        "telegram",
    ):
        assert topic in text
