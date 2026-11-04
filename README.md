# git-review

Review and edit a series of Git commits in VS Code. Each commit appears as unstaged changes; edit the files, then run `git review next` to save the result and open the next commit.

Requires Python 3.9+, Git 2.38+, and Linux or macOS. Uses only the Python standard library, with no additional dependencies.

## Installation

```bash
cd ~/git-review
python3 install.py
export PATH="$HOME/bin:$PATH"  # If ~/bin is not already on this shell's PATH
git review --version
```

The installer creates the command symlink at `~/bin/git-review`. The command works on its own; no AI tool or skill is required. You can also run `~/git-review/git-review` directly.

Skills are optional. By default, the installer detects Codex and Claude Code using their commands on PATH or their existing configuration directories, then installs a skill only for each detected tool:

| Tool | Command | Configuration directory | Skill destination |
| --- | --- | --- | --- |
| Codex | `codex` | `$CODEX_HOME`, defaulting to `~/.codex` | `<config>/skills/git-review` |
| Claude Code | `claude` | `$CLAUDE_CONFIG_DIR`, defaulting to `~/.claude` | `<config>/skills/git-review` |

If neither tool is detected, no skill is installed and no AI configuration directory is created. To skip skill installation even when AI tools are detected:

```bash
python3 install.py --no-skills
```

The installer never replaces an unrelated command or skill. A missing or unavailable skill destination does not prevent command installation: optional skill failures are reported and skipped. Re-running the installer is safe, and `--no-skills` leaves previously installed skills untouched. Claude Code's personal skill location and configuration override follow its [skills documentation](https://code.claude.com/docs/en/skills) and [environment variable reference](https://code.claude.com/docs/en/env-vars).

## Usage

In the repository you want to review, check out the target local branch and ensure the working tree is clean:

```bash
git review start <commit>
# Review and edit the current commit in VS Code; no manual commit is needed
git review next
# Keep editing and running next until the review is complete
git review status
```

`start` includes the specified commit and all subsequent commits through HEAD at the time the review starts. When a regular commit opens, HEAD points to its parent and the working tree contains its changes. New files use intent-to-add so they appear in the unstaged diff in both VS Code and `git diff`.

`next` automatically includes modifications and deletions of tracked files, along with files added by the original commit. Additional files you create during review require an explicit `git add`; otherwise, the tool lists them and pauses rather than carrying temporary files into the next commit. Staged changes are included too; partial staging boundaries are not preserved.

The original commit message, author, and author timestamp are preserved by default. If both the content and parent are unchanged, the original commit is reused. Editing a commit changes its SHA and the SHAs of its descendants. Commits that you make empty, or that become empty during replay, are kept so each original commit has one review result.

The final `next` completes the review and updates the original local branch. The tool does not push. The original history backup and the mapping between old and new SHAs are stored in Git metadata.

## Conflicts

If an edited commit conflicts with the next commit, the previous commit has already been saved. The tool stops at the next commit and lists affected files and marker line numbers. Ordinary text conflicts preserve both sides using Git's standard markers:

```text
    <<<<<<< HEAD
    The side that has already been reviewed
    =======
    The next commit being applied
    >>>>>>> commit
```

Edit the files to their intended final content in VS Code, remove the markers, and run `git review next`. The tool stages the resolution automatically and saves it in the commit that encountered the conflict. Remaining markers block the commit even if you have manually run `git add`.

Binary, modify/delete, rename, and other conflicts that cannot be resolved through ordinary text markers are listed separately. Choose the final result, then use `git add <path>` or `git rm <path>` before running `next`. The tool does not choose a side for you.

## Status and the optional AI skill

```bash
git review status
git review list
git review status --json
```

`git review list` shows the ordered commit list and a summary of completed and remaining commits. The remaining count includes the current commit. Each row shows its position, status, original SHA, and subject; `>` marks the current commit. A conflicting commit is labeled `conflict`:

```text
Branch: feature | Phase: reviewing
Total: 3 | Completed: 1 | Remaining: 2 (including current)

  1  done     a1b2c3d4e5f6 - Add parser
> 2  current  b2c3d4e5f6a1 - Handle empty input
  3  pending  c3d4e5f6a1b2 - Add regression coverage
```

Listing commits does not change files, staging, HEAD, or review progress. If no review is active, it prints a message explaining how to start one.

The JSON output includes `active`, `phase`, the current commit and its position, remaining commits, the original branch, saved SHA mappings, conflict files and marker lines, changed files, and the backup ref. With no active session, it returns `active: false` and exits successfully. Unexpected Git metadata or external operations cause the tool to report an error and preserve the current state.

The optional [git-review skill](skills/git-review/SKILL.md) tells an AI agent to check the session state before editing the current commit. Invoke it with `$git-review` in Codex or `/git-review` in Claude Code; for example, ask it to edit the current commit, then run next. The skill does not depend on context from an earlier conversation. A request to edit files alone does not authorize advancing the review. An ordinary Git rebase is not treated as a git-review session. All command-line review operations work without the skill.

When working over SSH, run commands on the host and in the repository that contain the actual review. A non-login SSH session may not include `~/bin` on PATH; use `~/git-review/git-review status --json` in that case. If you want skill assistance from a local AI while editing a remote repository, optionally install this project's `skills/git-review/` directory in the local AI's skill directory.

## Aborting and recovering

```bash
git review abort
```

Before aborting, the tool uses a separate temporary index to save the current files, including conflict markers and non-ignored untracked files, in a recovery snapshot. It does not modify the real index while making the snapshot. Git then restores the original branch. The snapshot's parent retains commits already saved during review. The tool does not run `git clean`; additional untracked files may remain in the working tree.

Use the refs printed by the tool to inspect or restore files:

```bash
git show <recovery-ref>:path/to/file
git restore --source=<recovery-ref> -- path/to/file
git log <backup-ref>
```

Session state lives in `git-review/` under the directory reported by `git rev-parse --absolute-git-dir`. Each linked worktree has its own state and lock. Completed and aborted session records live in `git-review/history/`. Backups use `refs/git-review/backups/` and `refs/git-review/recovery/` and are not automatically deleted.

You can close the terminal and continue later. If a commit hook fails, your edits remain available; fix the issue and retry `next`. After an interrupted Git operation, check `status` first. The tool records key phases so operations can be retried at known boundaries. Manually switching branches, committing, resetting, changing the original branch ref, or manipulating the rebase breaks the session's assumptions. The tool stops when it detects an inconsistency rather than guessing how to proceed. Automatic recovery from every possible forced termination or external Git operation is not guaranteed.

## Prototype scope

- Supports contiguous linear histories, regular files, executable bits, symlinks, and linked worktrees.
- Rejects ranges containing merge commits, reviews starting at a root commit, submodules, sparse checkout, and special index flags such as assume-unchanged or skip-worktree before starting.
- Uses Git's interactive rebase and merge machinery. Respects commit hooks, identity, and signing configuration; rewritten commits cannot retain the original signatures.
- Ignored files are excluded from commits and recovery snapshots. Do not rely on the tool to back up work stored in ignored paths.
- Does not provide an interface for splitting, squashing, reordering, or rewording commits.

## Development

```bash
python3 -m unittest discover -s tests -v
```

Tests use isolated temporary repositories with synthetic fixtures. They do not modify real development repositories.
