import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERNS = [r"\d{8,10}:AA[\w-]{30,}", r"AIza[\w-]{30,}", r"AQ\.[\w-]{30,}"]


def test_no_secrets_in_tracked_files():
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    for f in files:
        p = ROOT / f
        if not p.is_file() or p.suffix in (".png", ".zip"):
            continue
        text = p.read_text(errors="ignore")
        for pat in PATTERNS:
            assert not re.search(pat, text), f"secret-like string in {f}"


def test_env_is_ignored():
    assert ".env" in (ROOT / ".gitignore").read_text().split()
