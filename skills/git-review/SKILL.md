---
name: git-review
description: Review, edit, and navigate Git commits with the git-review tool. Use when the user mentions git review, reviewing commits individually, next or prev in a git-review session, or editing its current commit. Inspect status --json and handle conflicts without taking over an ordinary rebase.
---

# Git Review

## Identify the actual session

Run `git review status --json` in the repository specified by the user. When working over SSH, run it in the actual remote repository. If the command is unavailable in a non-login SSH session, use `~/git-review/git-review status --json`. Do not substitute local repository state for remote state.

Use `git review list` for an ordered overview of completed, current, pending, and preserved draft commits. The remaining count includes the current commit; a conflicting commit is labeled `conflict`. This command only reads progress. A request to list commits or check progress does not authorize running `next` or `prev`.

- `active: false`: There is no active session managed by this tool. Run `git review start <commit>` only when the user asks to start a review; do not turn an ordinary editing request into a history rewrite. An explicit `prev` request may reopen a completed review if the branch still matches its result and the worktree is clean.
- `active: true`: Read `phase`, `current`, `completed`, `remaining`, `drafts`, `conflicts`, and `changed_files` to identify the commit being edited. Use `git diff HEAD` for the actual edits and `git show <current.replay_sha>` for the saved version or draft being replayed (fall back to `current.sha` with older tool versions). `current.sha` remains the original commit identity after moving back.
- `reviewing`: HEAD is the current commit's parent, and the working tree contains the full set of changes under review. These uncommitted changes are part of the workflow; do not discard them to "clean up" the repository.
- `conflict`: The previous commit has been saved. The files now contain conflicts from applying the next commit. The resolution belongs to `current`, not the previous commit.
- `starting`, `expanding`, `saving`, `saved`, `advancing`, or `aborting`: An operation may have been interrupted. Inspect the tool's state and `git status` first. Within the existing authorization, use the tool's own `next` or `abort` to recover. If state is inconsistent, preserve it and report the issue; do not guess a reset target.
- `rewinding`: A `prev` operation may have been interrupted. Within the existing authorization, retry `prev` or `next` to finish opening the earlier commit. This recovery step does not save that commit or advance past it.

## Edit and advance

1. Edit the current working tree as requested and perform validation appropriate to the change. Preserve existing changes outside the task's scope.
2. Tracked files and files added by the original commit are included automatically. For additional files required by the current fix, explicitly run `git add -- <paths>`. Do not use an indiscriminate `git add -A` that could include temporary files.
3. Run `git review next` only when the user requests advancement or has authorized an edit-then-next or sequential-review workflow. If the user only asks for edits, stay on the current commit. Do not ask again for advancement that is already authorized.
4. Read the command output and query `status --json` again. Report the saved commit, new progress, and any conflicts. After the final commit, `active` becomes false and the original branch has been updated.

**Let the tool manage history during an active session.** Do not manually run `git commit`, `commit --amend`, `reset`, `switch`, `checkout <commit>`, `rebase --continue/--abort`, or another cherry-pick. Targeted `git add` and `git rm` are allowed for additional files and conflicts requiring an explicit resolution. Use `git review abort` to cancel a review, and only when the user asks to cancel.

## Revisit a saved commit

Run `git review prev` when the user asks to return to the previous commit. It preserves the current edits in a draft and opens the previous saved version as unstaged changes, including edits saved by earlier `next` calls. Repeated `prev` calls move further back within the review range. The command reports an error without moving when already at the first commit.

After moving back, read `status --json` again before editing. Do not assume the original SHA's tree contains the user's saved changes. Report the reopened commit and any files identified in errors or conflicts. Additional untracked files stay in place; they are not automatically committed. Move a blocking file only within the user's authorization, without discarding it.

`next` saves the reopened commit and reapplies the preserved version of the following commit. This may create conflicts, and already unresolved conflicts in a draft still require resolution. Editing permission and advancement permission remain separate: a request to go back and inspect does not itself authorize edits or running `next`.

## Conflicts

When the tool reports conflicts, explicitly list the affected files for the user. Do not merely report that a conflict occurred. `conflicts[].marker_lines` lists remaining marker lines, and `needs_manual_stage` identifies conflicts requiring an explicit choice.

- Ordinary text conflicts preserve both sides with `<<<<<<<`, `=======`, and `>>>>>>>`. The HEAD side is the history already reviewed; the other side is the next commit being applied. Resolve the conflict using the intent of both changes rather than automatically choosing ours or theirs.
- After editing the final text and removing the markers, `next` stages and saves the resolution automatically. There is no separate `continue` command.
- Binary, modify/delete, and similar conflicts require choosing the final file and then using targeted `git add` or `git rm`. Do not run `next` expecting the tool to choose the result.
- If conflict resolution is already authorized, resolve it and advance within that authorization. If authorization only covers the previous edit, report the new conflict and affected files and wait for instructions rather than expanding the scope.
- When `next` reports remaining markers or a hook failure, edits are preserved. Fix the specific issue and retry; do not bypass checks or force a reset to continue.

This prototype supports contiguous linear histories only. The original history has a backup ref, `prev` retains drafts under `refs/git-review/drafts/`, and `abort` saves a recovery snapshot. Those backups do not authorize discarding work. The tool does not push; publishing or uploading requires separate user authorization.
