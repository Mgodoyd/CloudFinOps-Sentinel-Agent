"""No credential may live in a tracked file.

A Gemini API key sat in `config.py` as a field default for weeks. It shipped to
every clone and every fork, and taking it out of the file does not take it out
of the history — the only real fix is rotating the key. This is the check that
would have caught it on the first commit.

Deliberately a test rather than a lint rule or a hook: it runs wherever the
suite runs, including CI, and it fails the build rather than printing a warning
somebody scrolls past.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Shapes that are credentials rather than examples. Placeholders and the obvious
# documentation stand-ins are allowed: a README that cannot show the shape of a
# key is a README nobody can follow.
SECRET_SHAPES = [
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("Google OAuth / AI Studio key", re.compile(r"\bAQ\.[0-9A-Za-z_\-]{20,}")),
    ("Telegram bot token", re.compile(r"\b\d{8,12}:AA[0-9A-Za-z_\-]{30,}")),
    ("Slack webhook", re.compile(r"https://hooks\.slack\.com/services/T[0-9A-Z]{6,}/B[0-9A-Z]{6,}/\w{20,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

ALLOWED = re.compile(
    r"(XXXX|xxxx|your-|YOUR_|<|\.\.\.|example|EXAMPLE|placeholder|fake|test|dummy)"
)


def tracked_files():
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    for name in out.stdout.splitlines():
        path = ROOT / name
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".pdf", ".ico", ".gif"}:
            continue
        if path.is_file():
            yield name, path


def test_no_credential_is_committed():
    findings = []
    for name, path in tracked_files():
        if name.startswith("tests/test_no_committed_secrets"):
            continue  # the patterns themselves live here
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, pattern in SECRET_SHAPES:
            for match in pattern.finditer(text):
                line = text[: match.start()].count("\n") + 1
                context = text.splitlines()[line - 1]
                if ALLOWED.search(context):
                    continue
                findings.append(f"{name}:{line} looks like a {label}")

    assert not findings, (
        "A credential is committed. Rotate it first — removing it from the file "
        "does not remove it from the history:\n  " + "\n  ".join(findings)
    )


def test_the_api_key_has_no_default():
    """The specific regression. A default here is a committed credential."""
    from app.core.config import Settings

    assert Settings.model_fields["GEMINI_API_KEY"].default == "", (
        "GEMINI_API_KEY must default to empty; a key belongs in .env or Secret "
        "Manager. Empty degrades to heuristic mode, which is loud and harmless."
    )


def test_the_env_file_is_never_tracked():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    tracked = set(out.stdout.split())
    assert ".env" not in tracked
    assert not [f for f in tracked if f.endswith(".json") and "service_account" in
                (ROOT / f).read_text(errors="ignore")[:200]]
