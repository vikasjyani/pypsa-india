"""Build the small, self-contained public Pages site."""

from __future__ import annotations

import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_GUIDE = ROOT / "docs" / "guide.html"
SOURCE_EXAMPLE = ROOT / "docs" / "examples" / "beginner_demo"
SOURCE_DOWNLOAD = ROOT / "docs" / "downloads" / "beginner_demo.zip"
SITE = ROOT / "_site"


def require_files() -> None:
    required = [SOURCE_GUIDE, SOURCE_DOWNLOAD]
    required.extend(SOURCE_EXAMPLE / name for name in ("README.md", "scenario_config.yaml", "beginner_demo.xlsx"))
    missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file()]
    if missing:
        raise SystemExit("Missing public site source files: " + ", ".join(missing))


def clean_site() -> None:
    expected = (ROOT / "_site").resolve()
    if expected != SITE.resolve() or expected.parent != ROOT.resolve() or expected.name != "_site":
        raise RuntimeError(f"Refusing to clean unexpected output path: {expected}")
    if SITE.is_symlink():
        raise RuntimeError(f"Refusing to clean symlink output: {SITE}")
    if SITE.exists():
        if not SITE.is_dir():
            raise RuntimeError(f"Refusing to replace non-directory output: {SITE}")
        shutil.rmtree(SITE)
    SITE.mkdir()


def copy_public_sources() -> None:
    shutil.copy2(SOURCE_GUIDE, SITE / "index.html")
    shutil.copy2(SOURCE_GUIDE, SITE / "guide.html")
    example_out = SITE / "examples" / "beginner_demo"
    example_out.mkdir(parents=True)
    for name in ("README.md", "scenario_config.yaml", "beginner_demo.xlsx"):
        source = SOURCE_EXAMPLE / name
        if source.is_symlink():
            raise RuntimeError(f"Refusing symlink source asset: {source}")
        shutil.copy2(source, example_out / name)
    (SITE / "downloads").mkdir()
    shutil.copy2(SOURCE_DOWNLOAD, SITE / "downloads" / SOURCE_DOWNLOAD.name)
    (SITE / ".nojekyll").touch()


if __name__ == "__main__":
    require_files()
    clean_site()
    copy_public_sources()
    print(f"Built public site at {SITE}")
