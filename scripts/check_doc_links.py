"""Check repository-local Markdown file targets without network access."""

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {".git", ".venv", "data", "build", "dist"}


def main():
    failures = []
    checked = 0
    for document in ROOT.rglob("*.md"):
        if set(document.relative_to(ROOT).parts) & EXCLUDED:
            continue
        content = document.read_text(encoding="utf-8")
        content = re.sub(r"```.*?```", "", content, flags=re.DOTALL)
        for raw in re.findall(r"\[[^\]]*\]\(([^\s)]+)(?:\s+[^)]*)?\)", content):
            parsed = urlsplit(raw.strip("<>"))
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            target = document.parent / unquote(parsed.path)
            checked += 1
            if not target.exists():
                failures.append(f"{document.relative_to(ROOT)}: missing {raw}")
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"Verified {checked} local Markdown targets (remote URLs and anchors excluded).")


if __name__ == "__main__":
    main()
