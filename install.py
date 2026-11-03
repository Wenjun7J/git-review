#!/usr/bin/env python3
"""Install local command and skill symlinks; never replace unrelated files."""
import os
from pathlib import Path
import sys


def main():
    project = Path(__file__).resolve().parent
    codex = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    links = [(Path.home() / "bin/git-review", project / "git-review"),
             (codex / "skills/git-review", project / "skills/git-review")]
    # Validate all destinations before changing any of them.
    for destination, source in links:
        if destination.exists() or destination.is_symlink():
            if not destination.is_symlink() or destination.resolve() != source.resolve():
                print(f"Refusing to replace existing path: {destination}", file=sys.stderr)
                return 1
    (project / "git-review").chmod(0o755)
    for destination, source in links:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.is_symlink():
            destination.symlink_to(source)
        print(f"Installed: {destination} -> {source}")
    if str(Path.home() / "bin") not in os.environ.get("PATH", "").split(os.pathsep):
        print('For this shell: export PATH="$HOME/bin:$PATH"')
    print("Usage: git review start <commit>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
