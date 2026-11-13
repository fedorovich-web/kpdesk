"""Never place real provider or owner secrets inside the distributable env template."""
import re
from pathlib import Path


def test_public_env_template_cannot_contain_live_credentials() -> None:
    path=Path(__file__).resolve().parents[2]/".env.example"
    content=path.read_text(encoding="utf-8")
    lines=dict(
        line.split("=",1)
        for line in content.splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    assert lines["OPENROUTER_API_KEY"] == ""
    assert "KP_DESK_LLM_API_KEY" not in lines
    assert not re.search(r"sk-or-v1-[A-Za-z0-9]{20,}",content)
    assert lines["KP_DESK_OWNER_TOKEN"] == ""
    assert not re.search(r"gsk_[A-Za-z0-9]{20,}",content)
    assert not re.search(r"AIza[0-9A-Za-z_-]{24,}",content)
    assert "-----BEGIN PRIVATE KEY-----" not in content

def test_future_private_git_excludes_originals_and_generated_evidence() -> None:
    """Repository preflight is local/static: NEVER run Git or stage files here."""
    root = Path(__file__).resolve().parents[2]
    ignore_lines = {
        line.strip() for line in (root / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert {
        ".env", "**/.env", "credentials/", "ml/data/real/", "ml/data/private/",
        "ml/data/incoming/", "ml/models/", "artifacts/", "backups/",
        "**/.venv/", "**/node_modules/", "**/dist/",
    } <= ignore_lines
