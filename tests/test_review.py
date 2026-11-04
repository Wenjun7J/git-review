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


if __name__ == "__main__":
    unittest.main()
