#!/usr/bin/env python3
"""Install the command and optional skills for detected AI tools."""
import argparse
import os
from pathlib import Path
import shutil
import sys


def install_link(destination, source):
    if destination.exists() or destination.is_symlink():
        if not destination.is_symlink() or destination.resolve() != source.resolve():
            raise FileExistsError(f"Refusing to replace existing path: {destination}")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(source)
    print(f"Installed: {destination} -> {source}")


def install_skills(project):
    source = project / "skills/git-review"
    if not (source / "SKILL.md").is_file():
        print("Skipping optional skills: the bundled SKILL.md is missing.", file=sys.stderr)
        return
    for name, command, variable, default in (
        ("Codex", "codex", "CODEX_HOME", ".codex"),
        ("Claude Code", "claude", "CLAUDE_CONFIG_DIR", ".claude"),
    ):
        try:
            config = Path(os.environ.get(variable) or str(Path.home() / default)).expanduser()
            if not shutil.which(command) and not config.is_dir():
                print(f"Skipping {name} skill: {name} was not detected.")
                continue
            install_link(config / "skills/git-review", source)
        except (OSError, RuntimeError) as error:
            print(f"Skipping optional {name} skill: {error}", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install git-review and optional AI skills.")
    parser.add_argument("--no-skills", action="store_true",
                        help="Install only the command, even if Codex or Claude Code is detected")
    args = parser.parse_args(argv)
    project = Path(__file__).resolve().parent
    try:
        (project / "git-review").chmod(0o755)
        install_link(Path.home() / "bin/git-review", project / "git-review")
    except (OSError, RuntimeError) as error:
        print(f"Command installation failed: {error}", file=sys.stderr)
        return 1
    if args.no_skills:
        print("Optional skill installation is disabled.")
    else:
        install_skills(project)
    if str(Path.home() / "bin") not in os.environ.get("PATH", "").split(os.pathsep):
        print('For this shell: export PATH="$HOME/bin:$PATH"')
    print("Usage: git review start <commit>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
