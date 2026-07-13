from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_METADATA = {"status", "owner", "last_verified", "verified_commit", "applies_to", "supersedes"}
LINK_PATTERN = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def main() -> int:
    errors: list[str] = []
    managed = [ROOT / "README.md", ROOT / "CHANGELOG.md", *sorted((ROOT / "docs").rglob("*.md"))]
    for path in managed:
        text = path.read_text(encoding="utf-8")
        metadata = parse_frontmatter(text)
        missing = REQUIRED_METADATA - set(metadata)
        if missing:
            errors.append(f"{path.relative_to(ROOT)}: missing metadata {sorted(missing)}")
        for target in LINK_PATTERN.findall(text):
            target = target.strip().strip("<>").split("#", 1)[0]
            if not target or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE):
                continue
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                errors.append(f"{path.relative_to(ROOT)}: broken link {target}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"[PASS] checked {len(managed)} managed Markdown files")
    return 0


def parse_frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return {}
    result: dict[str, str] = {}
    for line in lines[1:]:
        if line == "---":
            break
        if ":" in line:
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
