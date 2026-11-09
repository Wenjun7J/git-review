"""Exercise installer side effects in isolated homes and project directories."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("installer", Path(__file__).resolve().parents[1] / "install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="git-review-install-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.test_home = self.root / "home"
        self.test_home.mkdir()
        self.executables = self.root / "executables"
        self.executables.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        self.command = self.project / "git-review"
        self.command.write_text("#!/usr/bin/env python3\n")
        self.skill = self.project / "skills/git-review"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("---\nname: git-review\ndescription: Review commits.\n---\n")
        for replacement in (
            patch.object(installer, "__file__", str(self.project / "install.py")),
            patch.object(installer.Path, "home", return_value=self.test_home),
            patch.dict(os.environ, {"PATH": str(self.executables)}, clear=True),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def add_executable(self, name):
        command = self.executables / name
        command.write_text("#!/bin/sh\nexit 97\n")
        command.chmod(0o755)

    def run_install(self, *args):
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            return installer.main(list(args))

    def assert_command_installed(self):
        target = self.test_home / "bin/git-review"
        self.assertTrue(target.is_symlink())
        self.assertEqual(target.resolve(), self.command)
        self.assertTrue(os.access(target, os.X_OK))

    def assert_skill_installed(self, config):
        target = config / "skills/git-review"
        self.assertTrue(target.is_symlink())
        self.assertEqual(target.resolve(), self.skill)
        self.assertEqual((target / "SKILL.md").read_bytes(), (self.skill / "SKILL.md").read_bytes())

    def test_no_ai_tools_installs_only_command(self):
        self.assertEqual(self.run_install(), 0)
        self.assert_command_installed()
        self.assertFalse((self.test_home / ".codex").exists())
        self.assertFalse((self.test_home / ".claude").exists())

    def test_codex_executable_installs_only_codex_skill(self):
        self.add_executable("codex")
        self.assertEqual(self.run_install(), 0)
        self.assert_command_installed()
        self.assert_skill_installed(self.test_home / ".codex")
        self.assertFalse((self.test_home / ".claude").exists())

    def test_claude_executable_installs_only_claude_skill(self):
        self.add_executable("claude")
        self.assertEqual(self.run_install(), 0)
        self.assert_command_installed()
        self.assert_skill_installed(self.test_home / ".claude")
        self.assertFalse((self.test_home / ".codex").exists())

    def test_existing_configs_without_commands_install_both_idempotently(self):
        for name in (".codex", ".claude"):
            (self.test_home / name).mkdir()
        self.assertEqual(self.run_install(), 0)
        self.assertEqual(self.run_install(), 0)
        self.assert_command_installed()
        self.assert_skill_installed(self.test_home / ".codex")
        self.assert_skill_installed(self.test_home / ".claude")

    def test_no_skills_flag_skips_detected_tools(self):
        self.add_executable("codex")
        self.add_executable("claude")
        self.assertEqual(self.run_install("--no-skills"), 0)
        self.assert_command_installed()
        self.assertFalse((self.test_home / ".codex").exists())
        self.assertFalse((self.test_home / ".claude").exists())

    def test_no_skills_flag_preserves_existing_skills(self):
        self.add_executable("codex")
        self.add_executable("claude")
        self.assertEqual(self.run_install(), 0)
        self.assertEqual(self.run_install("--no-skills"), 0)
        self.assert_skill_installed(self.test_home / ".codex")
        self.assert_skill_installed(self.test_home / ".claude")

    def test_custom_config_directories_are_respected(self):
        codex = self.root / "codex-profile"
        claude = self.root / "claude-profile"
        codex.mkdir()
        claude.mkdir()
        with patch.dict(os.environ, {"CODEX_HOME": str(codex), "CLAUDE_CONFIG_DIR": str(claude)}):
            self.assertEqual(self.run_install(), 0)
        self.assert_skill_installed(codex)
        self.assert_skill_installed(claude)
        self.assertFalse((self.test_home / ".codex").exists())
        self.assertFalse((self.test_home / ".claude").exists())

    def test_unset_up_custom_configs_do_not_count_as_installed_tools(self):
        codex = self.root / "missing-codex"
        claude = self.root / "missing-claude"
        with patch.dict(os.environ, {"CODEX_HOME": str(codex), "CLAUDE_CONFIG_DIR": str(claude)}):
            self.assertEqual(self.run_install(), 0)
        self.assert_command_installed()
        self.assertFalse(codex.exists())
        self.assertFalse(claude.exists())

    def test_conflicting_skill_is_preserved_without_blocking_other_installs(self):
        existing = self.test_home / ".codex/skills/git-review"
        existing.mkdir(parents=True)
        (existing / "SKILL.md").write_text("Existing user skill.\n")
        self.add_executable("claude")
        self.assertEqual(self.run_install(), 0)
        self.assert_command_installed()
        self.assertEqual((existing / "SKILL.md").read_text(), "Existing user skill.\n")
        self.assertFalse(existing.is_symlink())
        self.assert_skill_installed(self.test_home / ".claude")
        self.assertTrue(self.stderr.getvalue())

    def test_unavailable_skill_directory_does_not_block_command(self):
        config = self.test_home / ".codex"
        config.mkdir()
        (config / "skills").write_text("An existing file.\n")
        self.assertEqual(self.run_install(), 0)
        self.assert_command_installed()
        self.assertEqual((config / "skills").read_text(), "An existing file.\n")
        self.assertTrue(self.stderr.getvalue())

    def test_conflicting_command_is_preserved_and_no_skills_are_installed(self):
        target = self.test_home / "bin/git-review"
        target.parent.mkdir()
        target.write_text("An existing command.\n")
        self.add_executable("codex")
        self.add_executable("claude")
        self.assertEqual(self.run_install(), 1)
        self.assertEqual(target.read_text(), "An existing command.\n")
        self.assertFalse(target.is_symlink())
        self.assertFalse((self.test_home / ".codex").exists())
        self.assertFalse((self.test_home / ".claude").exists())

    def test_missing_bundled_skill_does_not_block_command(self):
        (self.skill / "SKILL.md").unlink()
        self.add_executable("codex")
        self.add_executable("claude")
        self.assertEqual(self.run_install(), 0)
        self.assert_command_installed()
        self.assertFalse((self.test_home / ".codex").exists())
        self.assertFalse((self.test_home / ".claude").exists())


if __name__ == "__main__":
    unittest.main()
