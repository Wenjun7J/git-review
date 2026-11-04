---
name: git-review
description: Review, edit, and advance Git commits with the git-review tool. Use when the user mentions git review, reviewing commits individually, editing the current commit then running next, or a confirmed git-review session. Inspect status --json and handle conflicts without taking over an ordinary rebase.
---

# Git Review

## Identify the actual session

Run `git review status --json` in the repository specified by the user. When working over SSH, run it in the actual remote repository. If the command is unavailable in a non-login SSH session, use `~/git-review/git-review status --json`. Do not substitute local repository state for remote state.

Use `git review list` for an ordered overview of completed, current, and pending commits. The remaining count includes the current commit; a conflicting commit is labeled `conflict`. This command only reads progress. A request to list commits or check progress does not authorize running `next`.

- `active: false`: There is no session managed by this tool. Run `git review start <commit>` only when the user asks to start a review; do not turn an ordinary editing request into a history rewrite.
- `active: true`: Read `phase`, `current`, `completed`, `remaining`, `conflicts`, and `changed_files` to identify the commit being edited. Use `git diff HEAD` and `git show <current.sha>` for additional context.
- `reviewing`: HEAD is the current commit's parent, and the working tree contains the full set of changes under review. These uncommitted changes are part of the workflow; do not discard them to "clean up" the repository.
- `conflict`: The previous commit has been saved. The files now contain conflicts from applying the next commit. The resolution belongs to `current`, not the previous commit.
- `starting`, `expanding`, `saving`, `saved`, `advancing`, or `aborting`: An operation may have been interrupted. Inspect the tool's state and `git status` first. Within the existing authorization, use the tool's own `next` or `abort` to recover. If state is inconsistent, preserve it and report the issue; do not guess a reset target.

## Edit and advance

1. Edit the current working tree as requested and perform validation appropriate to the change. Preserve existing changes outside the task's scope.
2. Tracked files and files added by the original commit are included automatically. For additional files required by the current fix, explicitly run `git add -- <paths>`. Do not use an indiscriminate `git add -A` that could include temporary files.
3. Run `git review next` only when the user requests advancement or has authorized an edit-then-next or sequential-review workflow. If the user only asks for edits, stay on the current commit. Do not ask again for advancement that is already authorized.
4. Read the command output and query `status --json` again. Report the saved commit, new progress, and any conflicts. After the final commit, `active` becomes false and the original branch has been updated.

**Let the tool manage history during an active session.** Do not manually run `git commit`, `commit --amend`, `reset`, `switch`, `checkout <commit>`, `rebase --continue/--abort`, or another cherry-pick. Targeted `git add` and `git rm` are allowed for additional files and conflicts requiring an explicit resolution. Use `git review abort` to cancel a review, and only when the user asks to cancel.

## Conflicts

When the tool reports conflicts, explicitly list the affected files for the user. Do not merely report that a conflict occurred. `conflicts[].marker_lines` lists remaining marker lines, and `needs_manual_stage` identifies conflicts requiring an explicit choice.

- Ordinary text conflicts preserve both sides with `<<<<<<<`, `=======`, and `>>>>>>>`. The HEAD side is the history already reviewed; the other side is the next commit being applied. Resolve the conflict using the intent of both changes rather than automatically choosing ours or theirs.
- After editing the final text and removing the markers, `next` stages and saves the resolution automatically. There is no separate `continue` command.
- Binary, modify/delete, and similar conflicts require choosing the final file and then using targeted `git add` or `git rm`. Do not run `next` expecting the tool to choose the result.
- If conflict resolution is already authorized, resolve it and advance within that authorization. If authorization only covers the previous edit, report the new conflict and affected files and wait for instructions rather than expanding the scope.
- When `next` reports remaining markers or a hook failure, edits are preserved. Fix the specific issue and retry; do not bypass checks or force a reset to continue.

This prototype supports contiguous linear histories only. The original history has a backup ref, and `abort` saves a recovery snapshot. Those backups do not authorize discarding work. The tool does not push; publishing or uploading requires separate user authorization.
