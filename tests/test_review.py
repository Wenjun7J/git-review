"""Integration tests: every test operates in an isolated, synthetic Git repository."""
import json
import fcntl
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest

CLI = Path(__file__).resolve().parents[1] / "git-review"


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="git-review-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repository"
        self.repo.mkdir()
        self.env = os.environ.copy()
        for name in list(self.env):
            if name.startswith("GIT_"):
                del self.env[name]
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                        GIT_AUTHOR_DATE="2024-01-02T03:04:05+0000",
                        GIT_COMMITTER_DATE="2024-02-03T04:05:06+0000",
                        LC_ALL="C.UTF-8")
        self.git("init", "-b", "feature")
        self.git("config", "user.name", "Example Developer")
        self.git("config", "user.email", "developer@example.invalid")
        self.write("base.txt", "base\n")
        self.base = self.commit("base")

    def run_command(self, command, ok=True, cwd=None, env=None):
        result = subprocess.run(command, cwd=cwd or self.repo, env=env or self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def git(self, *args, **kwargs):
        return self.run_command(["git", *args], **kwargs).stdout.strip()

    def review(self, *args, **kwargs):
        return self.run_command([sys.executable, str(CLI), *args], **kwargs)

    def write(self, name, content):
        file = self.repo / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)

    def commit(self, subject):
        self.git("add", "-A")
        self.git("commit", "-m", subject)
        return self.git("rev-parse", "HEAD")

    def status(self):
        return json.loads(self.review("status", "--json").stdout)

    def active_path(self):
        return Path(self.git("rev-parse", "--absolute-git-dir")) / "git-review" / "active.json"

    def fault_env(self, phase):
        shim_dir = self.root / "fault-injection"
        shim_dir.mkdir(exist_ok=True)
        wrapper = shim_dir / "git"
        wrapper.write_text(f"#!{sys.executable}\n" +
                           "import os, signal, subprocess, sys\n" +
                           f"result = subprocess.run([{shutil.which('git')!r}, *sys.argv[1:]])\n" +
                           "args = sys.argv[1:]\n" +
                           "phase = os.environ['REVIEW_TEST_FAULT']\n" +
                           "match = ((phase == 'commit' and args[0] == 'commit' and any(a.startswith('--reuse-message=') for a in args)) or\n" +
                           "         (phase == 'advance' and 'rebase' in args and '--continue' in args) or\n" +
                           "         (phase == 'rewind' and 'rebase' in args and '--skip' in args) or\n" +
                           "         (phase == 'rewind-head' and args[:3] == ['update-ref', '--no-deref', 'HEAD']) or\n" +
                           "         (phase == 'rewind-queue' and 'rebase' in args and '--edit-todo' in args) or\n" +
                           "         (phase == 'expand' and args[:3] == ['reset', '--mixed', '-N']))\n" +
                           "if match:\n    os.kill(os.getppid(), signal.SIGKILL)\n" +
                           "sys.exit(result.returncode)\n")
        wrapper.chmod(0o755)
        env = self.env.copy()
        env.update(PATH=str(shim_dir) + os.pathsep + self.env["PATH"], REVIEW_TEST_FAULT=phase)
        return env

    def conflicting_history(self):
        self.write("value.txt", "first\n")
        first = self.commit("first change")
        self.write("value.txt", "second\n")
        second = self.commit("second change")
        self.write("later.txt", "later\n")
        last = self.commit("third change")
        self.review("start", first)
        self.write("value.txt", "reviewed\n")
        result = self.review("next")
        return first, second, last, result

    def test_no_edits_preserve_all_hashes_and_new_files_are_unstaged(self):
        self.write("new.txt", "one\n")
        first = self.commit("add file")
        self.write("new.txt", "two\n")
        last = self.commit("modify file")
        self.review("start", first)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
        self.assertIn("new.txt", self.git("diff", "--name-only"))
        self.review("next")
        self.assertEqual(self.status()["current"]["sha"], last)
        self.review("next")
        self.assertFalse(self.status()["active"])
        self.assertEqual(self.git("rev-parse", "HEAD"), last)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("branch", "--show-current"), "feature")

    def test_edits_propagate_and_author_message_are_preserved(self):
        self.write("one.txt", "one\n")
        first = self.commit("first\n\nLonger explanation.")
        self.write("two.txt", "two\n")
        last = self.commit("second")
        self.review("start", first)
        self.write("one.txt", "improved\n")
        self.review("next")
        self.assertEqual((self.repo / "one.txt").read_text(), "improved\n")
        self.assertEqual(self.git("diff", "--name-only"), "two.txt")
        self.review("next")
        self.assertNotEqual(self.git("rev-parse", "HEAD"), last)
        self.assertEqual(self.git("rev-list", "--count", f"{self.base}..HEAD"), "2")
        self.assertEqual(self.git("show", "-s", "--format=%B%n%an%n%ae%n%aI", "HEAD^"),
                         self.git("show", "-s", "--format=%B%n%an%n%ae%n%aI", first))

    def test_conflict_lists_paths_blocks_markers_and_next_saves_resolution(self):
        first, second, last, result = self.conflicting_history()
        self.assertIn("value.txt:", result.stdout)
        state = self.status()
        self.assertEqual(state["phase"], "conflict")
        self.assertEqual(state["current"]["sha"], second)
        content = (self.repo / "value.txt").read_text()
        self.assertIn("reviewed", content)
        self.assertIn("second", content)
        self.assertIn("<<<<<<<", content)
        current_head = self.git("rev-parse", "HEAD")
        failed = self.review("next", ok=False)
        self.assertIn("value.txt:", failed.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), current_head)
        self.write("value.txt", "resolved\n")
        self.review("next")
        self.assertEqual(self.status()["current"]["sha"], last)
        self.assertEqual(self.git("show", "HEAD:value.txt"), "resolved")
        self.assertEqual(self.git("show", "HEAD^:value.txt"), "reviewed")
        self.review("next")
        self.assertFalse(self.status()["active"])

    def test_abort_preserves_conflict_snapshot_and_original_branch(self):
        _, _, last, _ = self.conflicting_history()
        conflict_content = (self.repo / "value.txt").read_text().strip()
        self.write("scratch.txt", "local notes\n")
        self.review("abort")
        self.assertEqual(self.git("rev-parse", "HEAD"), last)
        self.assertFalse(self.status()["active"])
        ref = self.git("for-each-ref", "--format=%(refname)", "refs/git-review/recovery/")
        self.assertEqual(self.git("show", ref + ":value.txt"), conflict_content)
        self.assertEqual(self.git("show", ref + ":scratch.txt"), "local notes")
        self.assertEqual(self.git("show", ref + "^:value.txt"), "reviewed")

    def test_original_added_files_removed_on_abort(self):
        self.write("new.txt", "one\n")
        first = self.commit("new file")
        (self.repo / "new.txt").unlink()
        last = self.commit("remove file")
        self.review("start", first)
        self.review("abort")
        self.assertEqual(self.git("rev-parse", "HEAD"), last)
        self.assertFalse((self.repo / "new.txt").exists())
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_untracked_extra_requires_explicit_add(self):
        self.write("one.txt", "one\n")
        first = self.commit("one")
        self.review("start", first)
        self.write("extra.txt", "extra\n")
        self.review("next", ok=False)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.git("add", "extra.txt")
        self.review("next")
        self.assertEqual(self.git("show", "HEAD:extra.txt"), "extra")

    def test_deletions_renames_modes_and_unusual_names(self):
        self.write("old name.txt", "rename me\n")
        self.write("obsolete.txt", "delete me\n")
        self.commit("setup")
        self.git("mv", "old name.txt", "new\tname.txt")
        (self.repo / "obsolete.txt").unlink()
        self.write("run.sh", "#!/bin/sh\nexit 0\n")
        (self.repo / "run.sh").chmod(0o755)
        (self.repo / "link").symlink_to("base.txt")
        first = self.commit("rename delete and add executable")
        self.review("start", first)
        self.write("new\tname.txt", "reviewed rename\n")
        self.review("next")
        self.assertEqual(self.git("show", "HEAD:new\tname.txt"), "reviewed rename")
        self.assertFalse((self.repo / "obsolete.txt").exists())
        self.assertTrue(self.git("ls-tree", "HEAD", "run.sh").startswith("100755"))
        self.assertTrue(self.git("ls-tree", "HEAD", "link").startswith("120000"))

    def test_empty_commits_are_kept(self):
        self.git("commit", "--allow-empty", "-m", "empty")
        empty = self.git("rev-parse", "HEAD")
        self.write("one.txt", "one\n")
        last = self.commit("one")
        self.review("start", empty)
        self.review("next")
        self.review("next")
        self.assertEqual(self.git("rev-parse", "HEAD"), last)

    def test_edit_that_empties_commit_keeps_commit(self):
        self.write("base.txt", "new\n")
        first = self.commit("change base")
        self.review("start", first)
        self.write("base.txt", "base\n")
        self.review("next")
        self.assertEqual(self.git("rev-list", "--count", f"{self.base}..HEAD"), "1")
        self.assertEqual(self.git("diff", "HEAD^", "HEAD"), "")

    def test_delete_modify_conflict_needs_explicit_resolution(self):
        self.write("base.txt", "first\n")
        first = self.commit("first")
        self.write("base.txt", "second\n")
        self.commit("second")
        self.review("start", first)
        (self.repo / "base.txt").unlink()
        self.review("next")
        self.assertEqual(self.status()["conflicts"][0]["kind"], "manual")
        self.review("next", ok=False)
        self.git("rm", "base.txt")
        self.review("next")
        self.assertFalse((self.repo / "base.txt").exists())

    def test_binary_conflict_needs_explicit_resolution(self):
        (self.repo / "blob.bin").write_bytes(b"a\0b")
        first = self.commit("first binary")
        (self.repo / "blob.bin").write_bytes(b"c\0d")
        self.commit("second binary")
        self.review("start", first)
        (self.repo / "blob.bin").write_bytes(b"e\0f")
        self.review("next")
        self.assertTrue(self.status()["conflicts"][0]["needs_manual_stage"])
        self.review("next", ok=False)
        (self.repo / "blob.bin").write_bytes(b"resolved\0binary")
        self.git("add", "blob.bin")
        self.review("next")
        self.assertEqual((self.repo / "blob.bin").read_bytes(), b"resolved\0binary")

    def test_dirty_start_is_rejected_without_changing_head(self):
        self.write("one.txt", "one\n")
        first = self.commit("one")
        self.write("base.txt", "dirty\n")
        self.review("start", first, ok=False)
        self.assertEqual(self.git("rev-parse", "HEAD"), first)
        self.assertEqual((self.repo / "base.txt").read_text(), "dirty\n")
        self.assertFalse(self.status()["active"])

    def test_detached_root_and_nonancestor_are_rejected(self):
        self.review("start", self.base, ok=False)
        self.write("one.txt", "one\n")
        first = self.commit("one")
        self.git("checkout", "--detach", first)
        self.review("start", first, ok=False)
        self.git("checkout", "feature")
        self.git("checkout", "-b", "other", self.base)
        self.write("other.txt", "other\n")
        other = self.commit("other")
        self.git("checkout", "feature")
        self.review("start", other, ok=False)
        self.assertEqual(self.git("rev-parse", "HEAD"), first)

    def test_merge_range_is_rejected(self):
        self.write("first.txt", "first\n")
        first = self.commit("first")
        self.git("checkout", "-b", "side", self.base)
        self.write("side.txt", "side\n")
        self.commit("side")
        self.git("checkout", "feature")
        self.git("merge", "--no-ff", "side", "-m", "merge")
        tip = self.git("rev-parse", "HEAD")
        self.review("start", first, ok=False)
        self.assertEqual(self.git("rev-parse", "HEAD"), tip)

    def test_linked_worktree_is_independent(self):
        self.write("one.txt", "one\n")
        first = self.commit("one")
        original_repo = self.repo
        linked = self.root / "linked worktree"
        self.git("worktree", "add", "-b", "review-branch", str(linked))
        self.repo = linked
        self.review("start", first)
        self.write("one.txt", "reviewed\n")
        self.review("next")
        self.assertEqual(self.git("branch", "--show-current"), "review-branch")
        self.assertEqual(self.git("rev-parse", "feature"), first)
        self.assertEqual((original_repo / "one.txt").read_text(), "one\n")

    def test_next_from_subdirectory(self):
        self.write("folder/one.txt", "one\n")
        first = self.commit("one")
        self.review("start", first)
        self.write("folder/one.txt", "reviewed\n")
        self.review("next", cwd=self.repo / "folder")
        self.assertEqual(self.git("show", "HEAD:folder/one.txt"), "reviewed")

    def test_failed_commit_hook_keeps_edits_for_retry(self):
        self.write("one.txt", "one\n")
        first = self.commit("one")
        self.review("start", first)
        self.write("one.txt", "reviewed\n")
        hook = self.repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.review("next", ok=False)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual((self.repo / "one.txt").read_text(), "reviewed\n")
        hook.unlink()
        self.review("next")
        self.assertEqual(self.git("show", "HEAD:one.txt"), "reviewed")

    def test_branch_movement_is_detected(self):
        self.write("one.txt", "one\n")
        first = self.commit("one")
        self.review("start", first)
        self.git("update-ref", "refs/heads/feature", self.base)
        self.review("next", ok=False)
        self.assertEqual(self.git("rev-parse", "feature"), self.base)
        self.git("update-ref", "refs/heads/feature", first)
        self.review("abort")

    def test_resume_after_expansion_state_written(self):
        self.write("one.txt", "one\n")
        first = self.commit("one")
        self.review("start", first)
        state = json.loads(self.active_path().read_text())
        state["phase"] = "expanding"
        self.active_path().write_text(json.dumps(state))
        self.review("next")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.status()["phase"], "reviewing")
        self.review("next")

    def test_staged_conflict_markers_still_block(self):
        self.conflicting_history()
        self.git("add", "value.txt")
        self.review("next", ok=False)
        self.assertEqual(self.status()["phase"], "conflict")

    def test_user_rebase_config_does_not_skip_reviews_or_move_other_branches(self):
        self.write("one.txt", "one\n")
        first = self.commit("one")
        self.git("branch", "other-ref")
        self.write("two.txt", "two\n")
        last = self.commit("fixup! one")
        self.git("config", "rebase.abbreviateCommands", "true")
        self.git("config", "rebase.autoSquash", "true")
        self.git("config", "rebase.updateRefs", "true")
        self.review("start", first)
        self.assertEqual(self.status()["total"], 2)
        self.write("one.txt", "reviewed\n")
        self.review("next")
        self.assertEqual(self.status()["current"]["sha"], last)
        self.review("next")
        self.assertEqual(self.git("rev-parse", "other-ref"), first)
        self.assertEqual(self.git("rev-list", "--count", f"{self.base}..HEAD"), "2")

    def test_replayed_commit_that_becomes_empty_is_still_reviewed(self):
        self.write("one.txt", "one\n")
        first = self.commit("one")
        self.write("one.txt", "two\n")
        second = self.commit("two")
        self.review("start", first)
        self.write("one.txt", "two\n")
        self.review("next")
        self.assertEqual(self.status()["current"]["sha"], second)
        self.assertEqual(self.git("diff", "HEAD"), "")
        self.review("next")
        self.assertEqual(self.git("rev-list", "--count", f"{self.base}..HEAD"), "2")

    def test_post_commit_hook_edits_are_reopened_for_review(self):
        self.write("one.txt", "one\n")
        first = self.commit("one")
        self.review("start", first)
        self.write("one.txt", "reviewed\n")
        hook = self.repo / ".git/hooks/post-commit"
        hook.write_text("#!/bin/sh\nprintf 'from hook\\n' > one.txt\n")
        hook.chmod(0o755)
        self.review("next", ok=False)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual((self.repo / "one.txt").read_text(), "from hook\n")
        hook.unlink()
        self.review("next")
        self.assertEqual(self.git("show", "HEAD:one.txt"), "from hook")

    def test_process_killed_after_commit_can_resume_without_duplicate_commit(self):
        self.write("one.txt", "one\n")
        first = self.commit("one")
        self.write("two.txt", "two\n")
        second = self.commit("two")
        self.review("start", first)
        self.write("one.txt", "reviewed\n")
        self.review("next", env=self.fault_env("commit"), ok=False)
        self.assertEqual(self.status()["phase"], "saving")
        self.review("next")
        self.assertEqual(self.status()["current"]["sha"], second)
        self.review("next")
        self.assertEqual(self.git("rev-list", "--count", f"{self.base}..HEAD"), "2")
        self.assertEqual(self.git("show", "HEAD:one.txt"), "reviewed")

    def test_process_killed_after_rebase_stop_resumes_correct_commit(self):
        self.write("one.txt", "one\n")
        first = self.commit("one")
        self.write("two.txt", "two\n")
        second = self.commit("two")
        self.review("start", first)
        self.review("next", env=self.fault_env("advance"), ok=False)
        self.assertEqual(self.status()["phase"], "advancing")
        self.review("next")
        self.assertEqual(self.status()["current"]["sha"], second)
        self.assertEqual(self.git("rev-parse", "HEAD"), first)
        self.review("next")

    def test_process_killed_after_final_rebase_can_finalize(self):
        self.write("one.txt", "one\n")
        first = self.commit("one")
        self.review("start", first)
        self.review("next", env=self.fault_env("advance"), ok=False)
        self.assertEqual(self.status()["phase"], "advancing")
        listing = self.review("list").stdout
        self.assertIn("Total: 1 | Completed: 1 | Remaining: 0", listing)
        self.assertFalse(any(line.startswith(">") for line in listing.splitlines()))
        self.review("next")
        self.assertFalse(self.status()["active"])
        self.assertEqual(self.git("rev-parse", "HEAD"), first)

    def test_process_killed_after_reset_keeps_unstaged_changes(self):
        self.write("one.txt", "one\n")
        first = self.commit("one")
        self.review("start", first, env=self.fault_env("expand"), ok=False)
        self.assertEqual(self.status()["phase"], "expanding")
        self.review("next")
        self.assertEqual(self.git("diff", "--name-only"), "one.txt")
        self.review("next")

    def test_concurrent_invocation_is_rejected(self):
        self.status()
        lock = self.repo / ".git/git-review/lock"
        with lock.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.review("status", ok=False)

    def test_text_add_add_conflict_resolves_without_manual_staging(self):
        self.write("one.txt", "one\n")
        first = self.commit("one")
        self.write("shared.txt", "later version\n")
        self.commit("add shared file")
        self.review("start", first)
        self.write("shared.txt", "review version\n")
        self.git("add", "shared.txt")
        self.review("next")
        conflict = self.status()["conflicts"][0]
        self.assertEqual(conflict["kind"], "text")
        self.assertFalse(conflict["needs_manual_stage"])
        self.write("shared.txt", "combined\n")
        self.review("next")
        self.assertEqual(self.git("show", "HEAD:shared.txt"), "combined")

    def test_submodule_in_range_base_is_rejected(self):
        self.git("update-index", "--add", "--cacheinfo", f"160000,{self.base},module")
        self.git("commit", "-m", "add module")
        self.git("update-index", "--force-remove", "module")
        self.git("commit", "-m", "remove module")
        last = self.git("rev-parse", "HEAD")
        self.review("start", last, ok=False)
        self.assertEqual(self.git("rev-parse", "HEAD"), last)
        self.assertFalse(self.status()["active"])

    def test_list_tracks_progress_without_changing_review_edits(self):
        self.assertIn("No active review", self.review("list").stdout)
        self.write("one.txt", "one\n")
        first = self.commit("first")
        self.write("two.txt", "two\n")
        second = self.commit("second")
        self.write("three.txt", "three\n")
        third = self.commit("third")
        self.review("start", first)
        listing = self.review("list").stdout
        self.assertIn("Total: 3 | Completed: 0 | Remaining: 3 (including current)", listing)
        rows = [line.split() for line in listing.splitlines() if " - " in line]
        self.assertEqual([row[-3] for row in rows], [first[:12], second[:12], third[:12]])
        self.assertEqual(rows[0][:3], [">", "1", "current"])
        self.write("one.txt", "staged edit\n")
        self.git("add", "one.txt")
        self.write("one.txt", "unstaged edit\n")
        state_before = self.active_path().read_bytes()
        index = Path(self.git("rev-parse", "--absolute-git-dir")) / "index"
        index_before = index.read_bytes()
        head_before = self.git("rev-parse", "HEAD")
        self.review("list")
        self.assertEqual(self.active_path().read_bytes(), state_before)
        self.assertEqual(index.read_bytes(), index_before)
        self.assertEqual(self.git("rev-parse", "HEAD"), head_before)
        self.assertEqual((self.repo / "one.txt").read_text(), "unstaged edit\n")
        self.review("next")
        listing = self.review("list").stdout
        self.assertIn("Total: 3 | Completed: 1 | Remaining: 2 (including current)", listing)
        rows = [line.split() for line in listing.splitlines() if " - " in line]
        self.assertEqual(rows[0][:2], ["1", "done"])
        self.assertEqual(rows[1][:3], [">", "2", "current"])
        self.assertEqual(rows[2][:2], ["3", "pending"])
        self.review("next")
        self.assertIn("Completed: 2 | Remaining: 1", self.review("list").stdout)
        self.review("next")
        self.assertIn("No active review", self.review("list").stdout)

    def test_list_counts_current_conflict_as_remaining(self):
        first, second, third, _ = self.conflicting_history()
        conflict_before = (self.repo / "value.txt").read_bytes()
        state_before = self.active_path().read_bytes()
        unmerged_before = self.git("ls-files", "--unmerged")
        listing = self.review("list").stdout
        self.assertIn("Completed: 1 | Remaining: 2 (including current)", listing)
        current_row = next(line.split() for line in listing.splitlines() if line.startswith(">"))
        self.assertEqual(current_row[:4], [">", "2", "conflict", second[:12]])
        self.assertEqual((self.repo / "value.txt").read_bytes(), conflict_before)
        self.assertEqual(self.active_path().read_bytes(), state_before)
        self.assertEqual(self.git("ls-files", "--unmerged"), unmerged_before)

    def test_list_does_not_count_an_already_saved_commit_as_remaining(self):
        self.write("one.txt", "one\n")
        first = self.commit("first")
        self.write("two.txt", "two\n")
        self.commit("second")
        self.review("start", first)
        self.write("one.txt", "edited\n")
        hook = self.repo / ".git/hooks/post-commit"
        hook.write_text("#!/bin/sh\nprintf 'temporary\\n' > scratch.txt\n")
        hook.chmod(0o755)
        self.review("next", ok=False)
        self.assertEqual(self.status()["phase"], "saved")
        listing = self.review("list").stdout
        self.assertIn("Completed: 1 | Remaining: 1", listing)
        self.assertFalse(any(line.startswith(">") for line in listing.splitlines()))

    def test_prev_reopens_saved_edits_and_replays_the_current_draft(self):
        self.write("one.txt", "original\n")
        first = self.commit("first\n\nOriginal message.")
        self.write("two.txt", "second\n")
        second = self.commit("second")
        self.review("start", first)
        self.write("one.txt", "saved edit\n")
        self.review("next")
        saved = self.git("rev-parse", "HEAD")
        self.write("two.txt", "unfinished edit\n")
        self.write("added.txt", "added while reviewing\n")
        self.git("add", "added.txt")
        self.review("prev")
        self.assertEqual(self.status()["current"]["sha"], first)
        self.assertEqual(self.status()["current"]["replay_sha"], saved)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
        self.assertEqual((self.repo / "one.txt").read_text(), "saved edit\n")
        self.assertFalse((self.repo / "two.txt").exists())
        self.assertIn("Completed: 0 | Remaining: 2", self.review("list").stdout)
        self.write("one.txt", "revised again\n")
        self.review("next")
        self.assertEqual(self.status()["current"]["sha"], second)
        self.assertEqual((self.repo / "one.txt").read_text(), "revised again\n")
        self.assertEqual((self.repo / "two.txt").read_text(), "unfinished edit\n")
        self.assertEqual((self.repo / "added.txt").read_text(), "added while reviewing\n")
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
        self.review("next")
        self.assertFalse(self.status()["active"])
        self.assertEqual(self.git("rev-list", "--count", f"{self.base}..HEAD"), "2")
        self.assertEqual(self.git("show", "HEAD:two.txt"), "unfinished edit")
        self.assertEqual(self.git("show", "-s", "--format=%B%n%an%n%ae%n%aI", "HEAD^"),
                         self.git("show", "-s", "--format=%B%n%an%n%ae%n%aI", first))
        self.assertEqual(self.git("show", "-s", "--format=%B%n%an%n%ae%n%aI", "HEAD"),
                         self.git("show", "-s", "--format=%B%n%an%n%ae%n%aI", second))

    def test_repeated_prev_keeps_later_saved_edits_and_unsaved_draft(self):
        commits = []
        for name in ("one", "two", "three"):
            self.write(name + ".txt", name + "\n")
            commits.append(self.commit(name))
        self.review("start", commits[0])
        for name in ("one", "two"):
            self.write(name + ".txt", name + " saved\n")
            self.review("next")
        self.write("three.txt", "three draft\n")
        self.review("prev")
        self.assertEqual((self.repo / "two.txt").read_text(), "two saved\n")
        self.write("two.txt", "two revised draft\n")
        self.review("prev")
        self.assertEqual((self.repo / "one.txt").read_text(), "one saved\n")
        self.review("next")
        self.assertEqual((self.repo / "two.txt").read_text(), "two revised draft\n")
        self.review("next")
        self.assertEqual((self.repo / "three.txt").read_text(), "three draft\n")
        self.review("next")
        self.assertFalse(self.status()["active"])
        self.assertEqual(self.git("rev-list", "--count", f"{self.base}..HEAD"), "3")

    def test_prev_during_text_conflict_preserves_markers_and_partial_resolution(self):
        first, second, _, _ = self.conflicting_history()
        self.write("value.txt", (self.repo / "value.txt").read_text() + "partial resolution\n")
        content = (self.repo / "value.txt").read_bytes()
        unmerged = self.git("ls-files", "--unmerged")
        self.review("prev")
        self.assertEqual(self.status()["current"]["sha"], first)
        self.assertEqual((self.repo / "value.txt").read_text(), "reviewed\n")
        self.review("next")
        self.assertEqual(self.status()["current"]["sha"], second)
        self.assertEqual((self.repo / "value.txt").read_bytes(), content)
        self.assertEqual(self.git("ls-files", "--unmerged"), unmerged)
        self.assertIn("value.txt:", self.review("next", ok=False).stderr)
        self.write("value.txt", "resolved\n")
        self.review("next")
        self.review("next")
        self.assertEqual(self.git("show", "HEAD:value.txt"), "resolved")

    def test_prev_edit_can_conflict_with_a_preserved_draft(self):
        self.write("base.txt", "first\n")
        first = self.commit("first")
        self.write("base.txt", "second\n")
        second = self.commit("second")
        self.review("start", first)
        self.review("next")
        self.write("base.txt", "unfinished second\n")
        self.review("prev")
        self.write("base.txt", "revised first\n")
        result = self.review("next")
        self.assertIn("base.txt:", result.stdout)
        self.assertEqual(self.status()["current"]["sha"], second)
        self.assertEqual(self.status()["phase"], "conflict")
        content = (self.repo / "base.txt").read_text()
        self.assertIn("revised first", content)
        self.assertIn("unfinished second", content)
        self.review("next", ok=False)
        self.write("base.txt", "combined\n")
        self.review("next")
        self.assertEqual(self.git("show", "HEAD:base.txt"), "combined")

    def test_prev_at_first_commit_and_without_session_preserves_state(self):
        self.review("prev", ok=False)
        self.write("one.txt", "one\n")
        first = self.commit("one")
        self.review("start", first)
        self.write("one.txt", "unsaved\n")
        before = self.active_path().read_bytes()
        self.assertIn("first commit", self.review("prev", ok=False).stderr)
        self.assertEqual(self.active_path().read_bytes(), before)
        self.assertEqual((self.repo / "one.txt").read_text(), "unsaved\n")

    def test_prev_after_completion_reopens_last_result_then_can_go_back_further(self):
        self.write("one.txt", "one\n")
        first = self.commit("one")
        self.write("two.txt", "two\n")
        second = self.commit("two")
        self.review("start", first)
        self.write("one.txt", "saved first\n")
        self.review("next")
        self.write("two.txt", "saved second\n")
        self.review("next")
        finished = self.git("rev-parse", "HEAD")
        self.review("prev")
        self.assertEqual(self.status()["current"]["sha"], second)
        self.assertEqual(self.status()["total"], 2)
        self.assertEqual((self.repo / "two.txt").read_text(), "saved second\n")
        self.review("prev")
        self.assertEqual((self.repo / "one.txt").read_text(), "saved first\n")
        self.review("next")
        self.review("next")
        self.assertEqual(self.git("rev-parse", "HEAD"), finished)

    def test_prev_does_not_reopen_a_completed_session_with_new_work(self):
        self.write("one.txt", "one\n")
        first = self.commit("one")
        self.review("start", first)
        self.review("next")
        self.write("one.txt", "unsaved\n")
        self.review("prev", ok=False)
        self.assertFalse(self.status()["active"])
        self.assertEqual((self.repo / "one.txt").read_text(), "unsaved\n")
        changed = self.commit("new work")
        self.review("prev", ok=False)
        self.assertEqual(self.git("rev-parse", "HEAD"), changed)

    def test_prev_keeps_untracked_files_out_of_saved_commits(self):
        self.write("one.txt", "one\n")
        first = self.commit("one")
        self.write("two.txt", "two\n")
        self.commit("two")
        self.review("start", first)
        self.review("next")
        self.write("notes.txt", "temporary notes\n")
        self.review("prev")
        self.assertEqual((self.repo / "notes.txt").read_text(), "temporary notes\n")
        self.assertIn("notes.txt", self.review("next", ok=False).stderr)
        (self.repo / "notes.txt").rename(self.root / "notes.txt")
        self.review("next")
        self.review("next")
        self.assertNotIn("notes.txt", self.git("ls-tree", "--name-only", "HEAD"))

    def test_prev_refuses_to_overwrite_ignored_or_untracked_files(self):
        self.write("old.txt", "tracked\n")
        self.base = self.commit("setup")
        (self.repo / "old.txt").unlink()
        first = self.commit("remove old file")
        self.write("two.txt", "two\n")
        self.commit("two")
        self.review("start", first)
        self.review("next")
        self.write("old.txt", "unrelated local file\n")
        state = self.active_path().read_bytes()
        index = self.repo / ".git/index"
        before = index.read_bytes()
        self.review("prev", ok=False)
        self.assertEqual(self.active_path().read_bytes(), state)
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual((self.repo / "old.txt").read_text(), "unrelated local file\n")
        (self.repo / ".git/info/exclude").write_text("old.txt\n")
        self.review("prev", ok=False)
        self.assertEqual(self.active_path().read_bytes(), state)
        self.assertEqual((self.repo / "old.txt").read_text(), "unrelated local file\n")

    def test_prev_preserves_binary_conflict_until_explicit_resolution(self):
        (self.repo / "data.bin").write_bytes(b"first\0content")
        first = self.commit("first")
        (self.repo / "data.bin").write_bytes(b"second\0content")
        self.commit("second")
        self.review("start", first)
        (self.repo / "data.bin").write_bytes(b"reviewed\0content")
        self.review("next")
        stages = self.git("ls-files", "--unmerged")
        self.review("prev")
        self.review("next")
        self.assertEqual(self.git("ls-files", "--unmerged"), stages)
        self.assertEqual((self.repo / "data.bin").read_bytes(), b"reviewed\0content")
        self.assertTrue(self.status()["conflicts"][0]["needs_manual_stage"])
        self.review("next", ok=False)
        (self.repo / "data.bin").write_bytes(b"resolved\0content")
        self.git("add", "data.bin")
        self.review("next")
        self.assertFalse(self.status()["active"])

    def test_prev_preserves_modify_delete_conflict(self):
        self.write("base.txt", "first\n")
        first = self.commit("first")
        self.write("base.txt", "second\n")
        self.commit("second")
        self.review("start", first)
        (self.repo / "base.txt").unlink()
        self.review("next")
        content = (self.repo / "base.txt").read_text()
        stages = self.git("ls-files", "--unmerged")
        self.review("prev")
        self.review("next")
        self.assertEqual((self.repo / "base.txt").read_text(), content)
        self.assertEqual(self.git("ls-files", "--unmerged"), stages)
        self.review("next", ok=False)
        self.git("rm", "base.txt")
        self.review("next")
        self.assertFalse((self.repo / "base.txt").exists())

    def test_abort_after_prev_keeps_future_draft_and_restores_original_branch(self):
        self.write("one.txt", "one\n")
        first = self.commit("one")
        self.write("two.txt", "two\n")
        original = self.commit("two")
        self.review("start", first)
        self.write("one.txt", "saved edit\n")
        self.review("next")
        self.write("two.txt", "unsaved draft\n")
        self.review("prev")
        draft = self.status()["drafts"][original]["ref"]
        self.review("abort")
        self.assertEqual(self.git("rev-parse", "HEAD"), original)
        self.assertEqual(self.git("show", draft + ":two.txt"), "unsaved draft")
        self.assertEqual(self.git("show", draft + "^:one.txt"), "saved edit")

    def test_prev_recovers_after_head_move_and_rebase_stop(self):
        self.write("one.txt", "one\n")
        first = self.commit("one")
        self.write("two.txt", "two\n")
        second = self.commit("two")
        self.review("start", first)
        self.review("next")
        for phase in ("rewind-queue", "rewind-head", "rewind"):
            with self.subTest(phase=phase):
                self.write("two.txt", phase + " draft\n")
                self.review("prev", env=self.fault_env(phase), ok=False)
                self.assertEqual(self.status()["phase"], "rewinding")
                self.review("next")
                self.assertEqual(self.status()["current"]["sha"], first)
                self.review("next")
                self.assertEqual(self.status()["current"]["sha"], second)
                self.assertEqual((self.repo / "two.txt").read_text(), phase + " draft\n")
        self.review("next")

    def test_prev_preserves_empty_commits_file_modes_and_symlinks(self):
        self.git("commit", "--allow-empty", "-m", "empty")
        first = self.git("rev-parse", "HEAD")
        self.write("folder/new\tfile.txt", "new\n")
        self.commit("new file")
        self.review("start", first)
        self.review("next")
        self.write("run.sh", "#!/bin/sh\nexit 0\n")
        (self.repo / "run.sh").chmod(0o755)
        (self.repo / "link").symlink_to("base.txt")
        self.git("add", "run.sh", "link")
        (self.repo / "base.txt").unlink()
        self.review("prev")
        self.assertEqual(self.git("diff", "HEAD"), "")
        self.review("next")
        self.assertFalse((self.repo / "base.txt").exists())
        self.assertEqual(os.readlink(self.repo / "link"), "base.txt")
        self.review("next", cwd=self.repo / "folder")
        self.assertEqual(self.git("rev-list", "--count", f"{self.base}..HEAD"), "2")
        self.assertTrue(self.git("ls-tree", "HEAD", "run.sh").startswith("100755"))

    def test_prev_in_linked_worktree_uses_its_own_state(self):
        self.write("one.txt", "one\n")
        first = self.commit("one")
        self.write("two.txt", "two\n")
        original = self.commit("two")
        original_repo = self.repo
        linked = self.root / "linked"
        self.git("worktree", "add", "-b", "review-copy", str(linked))
        self.repo = linked
        self.review("start", first)
        self.write("one.txt", "saved edit\n")
        self.review("next")
        self.review("prev")
        self.assertEqual((self.repo / "one.txt").read_text(), "saved edit\n")
        self.review("next")
        self.review("next")
        self.assertEqual(self.git("rev-parse", "feature"), original)
        self.assertEqual((original_repo / "one.txt").read_text(), "one\n")

    def test_prev_replays_saved_conflict_resolution_instead_of_original_commit(self):
        first, second, _, _ = self.conflicting_history()
        self.write("value.txt", "saved resolution\n")
        self.review("next")
        self.review("prev")
        self.assertEqual(self.status()["current"]["sha"], second)
        self.assertEqual((self.repo / "value.txt").read_text(), "saved resolution\n")
        self.review("prev")
        self.assertEqual(self.status()["current"]["sha"], first)
        self.write("value.txt", "revised earlier commit\n")
        self.review("next")
        content = (self.repo / "value.txt").read_text()
        self.assertIn("saved resolution", content)
        self.assertIn("revised earlier commit", content)
        self.assertIn("value.txt:", self.review("next", ok=False).stderr)
        self.write("value.txt", "new resolution\n")
        self.review("next")
        self.review("next")
        self.assertEqual(self.git("show", "HEAD:value.txt"), "new resolution")

    def test_prev_updates_the_ours_side_of_a_preserved_binary_conflict(self):
        (self.repo / "data.bin").write_bytes(b"first\0content")
        first = self.commit("first")
        (self.repo / "data.bin").write_bytes(b"second\0content")
        self.commit("second")
        self.review("start", first)
        (self.repo / "data.bin").write_bytes(b"reviewed\0content")
        self.review("next")
        self.review("prev")
        (self.repo / "data.bin").write_bytes(b"revised again\0content")
        self.review("next")
        self.assertEqual((self.repo / "data.bin").read_bytes(), b"revised again\0content")
        self.assertEqual(self.git("rev-parse", ":2:data.bin"), self.git("rev-parse", "HEAD:data.bin"))
        self.assertTrue(self.status()["conflicts"][0]["needs_manual_stage"])
        self.review("next", ok=False)
        self.git("add", "data.bin")
        self.review("next")
        self.assertFalse(self.status()["active"])

    def test_next_does_not_overwrite_ignored_files_when_replaying_a_draft(self):
        self.write("one.txt", "one\n")
        first = self.commit("one")
        self.write("two.txt", "two\n")
        self.commit("two")
        self.review("start", first)
        self.review("next")
        self.review("prev")
        (self.repo / ".git/info/exclude").write_text("two.txt\n")
        self.write("two.txt", "local ignored content\n")
        self.assertIn("two.txt", self.review("next", ok=False).stderr)
        self.assertEqual(self.status()["phase"], "advancing")
        self.assertEqual((self.repo / "two.txt").read_text(), "local ignored content\n")
        (self.repo / "two.txt").rename(self.root / "ignored.txt")
        self.review("next")
        self.assertEqual((self.repo / "two.txt").read_text(), "two\n")
        self.review("next")
        self.assertFalse(self.status()["active"])


if __name__ == "__main__":
    unittest.main()
