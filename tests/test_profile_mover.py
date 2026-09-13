#!/usr/bin/env python3
"""
Unit and Integration Tests for Claude Profile Mover / Cloner
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import claude_profile_mover as cpm


class TestClaudeProfileMover(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_claude_mover_"))
        self.src_user = "olduser"
        self.dest_user = "newuser"

        # Setup mock App Support directory structure
        self.mock_app_support = self.test_dir / "Library" / "Application Support"
        self.mock_claudework = self.mock_app_support / "ClaudeWork"
        self.mock_claude = self.mock_app_support / "Claude"
        self.mock_cli = self.test_dir / ".claude"

        self.mock_claudework.mkdir(parents=True, exist_ok=True)
        self.mock_claude.mkdir(parents=True, exist_ok=True)
        self.mock_cli.mkdir(parents=True, exist_ok=True)

        # Populate ClaudeWork config
        cw_config = {
            "coworkUserFilesPath": f"/Users/{self.src_user}/Claude",
            "mcpServers": {
                "filesystem": {
                    "command": f"/Users/{self.src_user}/.nvm/versions/node/v22/bin/node",
                    "args": [f"/Users/{self.src_user}/src/sample-project/index.js"]
                }
            }
        }
        with open(self.mock_claudework / "claude_desktop_config.json", "w") as f:
            json.dump(cw_config, f, indent=2)

        # Populate ClaudeWork sessions
        sess_dir = self.mock_claudework / "claude-code-sessions" / "uuid-account-123" / "sub-uuid-456"
        sess_dir.mkdir(parents=True, exist_ok=True)
        sample_session = {
            "sessionId": "session-abc-123",
            "cliSessionId": "cli-session-789",
            "title": "Sample Project Task",
            "cwd": f"/Users/{self.src_user}/src/sample-project",
            "originCwd": f"/Users/{self.src_user}/src/sample-project",
            "createdAt": 1789254515114,
        }
        with open(sess_dir / "local_session-abc-123.json", "w") as f:
            json.dump(sample_session, f, indent=2)

        # Create cache directory that should be ignored
        cache_dir = self.mock_claudework / "GPUCache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        with open(cache_dir / "data_0", "wb") as f:
            f.write(b"dummy cache binary data")

        # Populate ~/.claude CLI project with path-encoded directory name
        proj_dir = self.mock_cli / "projects" / f"-Users-{self.src_user}-src-sample-project"
        proj_dir.mkdir(parents=True, exist_ok=True)
        with open(proj_dir / "cli-session-789.jsonl", "w") as f:
            f.write(json.dumps({"cwd": f"/Users/{self.src_user}/src/sample-project", "msg": "hello"}) + "\n")
            f.write(json.dumps({"filePath": f"/Users/{self.src_user}/src/sample-project/index.js"}) + "\n")

        with open(self.mock_cli / "history.jsonl", "w") as f:
            f.write(json.dumps({"project": f"/Users/{self.src_user}/src/sample-project", "display": "test"}) + "\n")

        self.patcher = patch("claude_profile_mover.get_keychain_key", return_value=None)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_cache_exclusion(self):
        """Ensure ephemeral cache folders are excluded."""
        self.assertTrue(cpm.should_skip_path(Path("ClaudeWork/GPUCache/data_0")))
        self.assertTrue(cpm.should_skip_path(Path("ClaudeWork/DawnGraphiteCache")))
        self.assertTrue(cpm.should_skip_path(Path("ClaudeWork/Crashpad")))
        self.assertTrue(cpm.should_skip_path(Path("ClaudeWork/sentry")))
        self.assertTrue(cpm.should_skip_path(Path("ClaudeWork/test.sock")))
        self.assertFalse(cpm.should_skip_path(Path("ClaudeWork/claude_desktop_config.json")))
        self.assertFalse(cpm.should_skip_path(Path("ClaudeWork/claude-code-sessions/uuid/local_abc.json")))

    def test_path_rewriting(self):
        """Test safe in-place path replacements across files."""
        test_file = self.test_dir / "sample.json"
        content = {
            "user_dir": f"/Users/{self.src_user}/documents",
            "project": f"-Users-{self.src_user}-myproject",
            "unrelated": "something else"
        }
        with open(test_file, "w") as f:
            json.dump(content, f)

        replacements = [
            (f"/Users/{self.src_user}/", f"/Users/{self.dest_user}/"),
            (f"-Users-{self.src_user}-", f"-Users-{self.dest_user}-")
        ]
        cnt = cpm.rewrite_file_paths(test_file, replacements)
        self.assertEqual(cnt, 2)

        with open(test_file, "r") as f:
            updated = json.load(f)

        self.assertEqual(updated["user_dir"], f"/Users/{self.dest_user}/documents")
        self.assertEqual(updated["project"], f"-Users-{self.dest_user}-myproject")
        self.assertEqual(updated["unrelated"], "something else")

    def test_detect_source_username(self):
        """Test detecting username from JSON configs."""
        detected = cpm.detect_source_username(self.mock_claudework)
        self.assertEqual(detected, self.src_user)

    def test_collect_sessions_metadata(self):
        """Test session collection."""
        sessions = cpm.collect_sessions_metadata(self.mock_claudework)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["title"], "Sample Project Task")
        self.assertEqual(sessions[0]["id"], "local_session-abc-123")

    @patch("claude_profile_mover.find_active_claudework_dir")
    @patch("claude_profile_mover.CLAUDE_WORK_DIR")
    @patch("claude_profile_mover.CLAUDE_DIR")
    @patch("claude_profile_mover.CLAUDE_CLI_DIR")
    @patch("claude_profile_mover.check_running_processes", return_value=True)
    def test_export_and_import_cycle(self, mock_check_proc, mock_cli, mock_claude, mock_cw_dir, mock_find_cw):
        """Full end-to-end cycle: export ClaudeWork and import to target environment."""
        mock_find_cw.return_value = self.mock_claudework
        mock_cw_dir.__str__.return_value = str(self.mock_claudework)
        mock_claude.__str__.return_value = str(self.mock_claude)
        mock_cli.__str__.return_value = str(self.mock_cli)
        cpm.CLAUDE_WORK_DIR = self.mock_claudework
        cpm.CLAUDE_DIR = self.mock_claude
        cpm.CLAUDE_CLI_DIR = self.mock_cli

        archive_dest = self.test_dir / "test_cloned_profile.tar.gz"

        # 1. Export
        exported_path = cpm.do_export(
            profile_choice="claudework",
            output_archive=str(archive_dest),
            dry_run=False,
            include_cli=True,
        )
        self.assertIsNotNone(exported_path)
        self.assertTrue(archive_dest.exists())

        # 2. Setup simulated target Mac with new user
        target_dir = Path(tempfile.mkdtemp(prefix="target_mac_"))
        target_app_support = target_dir / "Library" / "Application Support" / "ClaudeWork"
        target_cli = target_dir / ".claude"
        target_app_support.mkdir(parents=True, exist_ok=True)
        target_cli.mkdir(parents=True, exist_ok=True)

        # Target Mac was logged in, has a fresh UUID
        (target_app_support / "claude-code-sessions" / "uuid-target-fresh").mkdir(parents=True, exist_ok=True)

        try:
            cpm.CLAUDE_WORK_DIR = target_app_support
            cpm.CLAUDE_CLI_DIR = target_cli
            mock_find_cw.return_value = target_app_support

            # 3. Import onto target Mac with remapping
            success = cpm.do_import(
                archive_path_str=str(archive_dest),
                from_user=self.src_user,
                to_user=self.dest_user,
                force=True,
                dry_run=False,
                skip_backup=True,
            )
            self.assertTrue(success)

            # Check that config was cloned and paths were updated
            target_cfg_file = target_app_support / "claude_desktop_config.json"
            self.assertTrue(target_cfg_file.exists())
            with open(target_cfg_file) as f:
                target_cfg = json.load(f)

            self.assertEqual(target_cfg["coworkUserFilesPath"], f"/Users/{self.dest_user}/Claude")
            self.assertIn(f"/Users/{self.dest_user}/.nvm", target_cfg["mcpServers"]["filesystem"]["command"])

            # Check that CLI project directory was renamed
            expected_proj_dir = target_cli / "projects" / f"-Users-{self.dest_user}-src-sample-project"
            self.assertTrue(expected_proj_dir.exists(), f"Expected {expected_proj_dir} to exist")

            # Check that project jsonl content has remapped paths
            target_jsonl = expected_proj_dir / "cli-session-789.jsonl"
            self.assertTrue(target_jsonl.exists())
            with open(target_jsonl) as f:
                jsonl_content = f.read()
            self.assertIn(f"/Users/{self.dest_user}/src/sample-project", jsonl_content)
            self.assertNotIn(f"/Users/{self.src_user}", jsonl_content)

        finally:
            shutil.rmtree(target_dir, ignore_errors=True)

    @patch("claude_profile_mover.find_active_claudework_dir")
    @patch("claude_profile_mover.CLAUDE_WORK_DIR")
    @patch("claude_profile_mover.CLAUDE_CLI_DIR")
    @patch("claude_profile_mover.check_running_processes", return_value=True)
    def test_clean_destination_removal(self, mock_check_proc, mock_cli, mock_cw_dir, mock_find_cw):
        """Verify that clean=True wipes old destination sessions and projects while preserving auth."""
        archive_dest = self.test_dir / "test_clean_archive.tar.gz"

        # 1. Export current profile
        mock_find_cw.return_value = self.mock_claudework
        mock_cw_dir.__str__.return_value = str(self.mock_claudework)
        mock_cli.__str__.return_value = str(self.mock_cli)
        cpm.CLAUDE_WORK_DIR = self.mock_claudework
        cpm.CLAUDE_CLI_DIR = self.mock_cli

        cpm.do_export(
            profile_choice="claudework",
            output_archive=str(archive_dest),
            dry_run=False,
            include_cli=True,
        )

        # 2. Simulate target Mac that already has an unwanted placeholder session & project
        target_dir = Path(tempfile.mkdtemp(prefix="target_mac_clean_"))
        target_app_support = target_dir / "Library" / "Application Support" / "ClaudeWork"
        target_cli = target_dir / ".claude"
        target_app_support.mkdir(parents=True, exist_ok=True)
        target_cli.mkdir(parents=True, exist_ok=True)

        target_uuid_dir = target_app_support / "claude-code-sessions" / "uuid-target" / "sub-target"
        target_uuid_dir.mkdir(parents=True, exist_ok=True)
        with open(target_uuid_dir / "local_old-placeholder-to-remove.json", "w") as f:
            json.dump({"title": "Old Garbage Session", "sessionId": "old-placeholder"}, f)

        # Pre-existing project to be removed
        old_proj = target_cli / "projects" / "-Users-newuser-old-junk-repo"
        old_proj.mkdir(parents=True, exist_ok=True)
        with open(old_proj / "dummy.json", "w") as f:
            f.write("{}")

        # Existing config.json with target's local keychain auth token
        with open(target_app_support / "config.json", "w") as f:
            json.dump({"oauth:tokenCache": "TARGET_MAC_KEYCHAIN_TOKEN_123"}, f)

        try:
            cpm.CLAUDE_WORK_DIR = target_app_support
            cpm.CLAUDE_CLI_DIR = target_cli
            mock_find_cw.return_value = target_app_support

            # 3. Import with clean=True
            success = cpm.do_import(
                archive_path_str=str(archive_dest),
                from_user=self.src_user,
                to_user=self.dest_user,
                clean=True,
                force=True,
                dry_run=False,
                skip_backup=True,
            )
            self.assertTrue(success)

            # Old placeholder session MUST BE REMOVED
            self.assertFalse((target_uuid_dir / "local_old-placeholder-to-remove.json").exists())

            # Old unwanted project folder MUST BE REMOVED
            self.assertFalse(old_proj.exists())

            # Cloned project MUST BE INSTALLED
            cloned_proj = target_cli / "projects" / f"-Users-{self.dest_user}-src-sample-project"
            self.assertTrue(cloned_proj.exists())

            # Target Mac's auth token MUST BE PRESERVED
            with open(target_app_support / "config.json") as f:
                res_cfg = json.load(f)
            self.assertEqual(res_cfg.get("oauth:tokenCache"), "TARGET_MAC_KEYCHAIN_TOKEN_123")

        finally:
            shutil.rmtree(target_dir, ignore_errors=True)

    @patch("claude_profile_mover.CLAUDE_DIR")
    @patch("claude_profile_mover.CLAUDE_CLI_DIR")
    @patch("claude_profile_mover.check_running_processes", return_value=True)
    def test_standard_claude_desktop_export(self, mock_check_proc, mock_cli, mock_claude):
        """Test default base-case export of standard Claude Desktop."""
        # Setup config in standard Claude dir
        claude_cfg = {"coworkUserFilesPath": f"/Users/{self.src_user}/Claude"}
        with open(self.mock_claude / "claude_desktop_config.json", "w") as f:
            json.dump(claude_cfg, f)

        mock_claude.__str__.return_value = str(self.mock_claude)
        mock_cli.__str__.return_value = str(self.mock_cli)
        cpm.CLAUDE_DIR = self.mock_claude
        cpm.CLAUDE_CLI_DIR = self.mock_cli

        archive_dest = self.test_dir / "test_claude_standard.tar.gz"

        # Default profile choice "claude"
        exported = cpm.do_export(
            profile_choice="claude",
            output_archive=str(archive_dest),
            dry_run=False,
            include_cli=False,
        )
        self.assertIsNotNone(exported)
        self.assertTrue(archive_dest.exists())

    @patch("claude_profile_mover.get_keychain_key")
    @patch("claude_profile_mover.CLAUDE_CLI_DIR")
    @patch("claude_profile_mover.CLAUDE_DIR")
    @patch("claude_profile_mover.check_running_processes")
    def test_export_no_key(self, mock_proc, mock_claude, mock_cli, mock_get_key):
        """Test that get_key=False skips calling get_keychain_key."""
        mock_claude.__str__.return_value = str(self.mock_claude)
        mock_cli.__str__.return_value = str(self.mock_cli)
        cpm.CLAUDE_DIR = self.mock_claude
        cpm.CLAUDE_CLI_DIR = self.mock_cli

        archive_dest = self.test_dir / "test_no_key.tar.gz"
        cpm.do_export(
            profile_choice="claude",
            output_archive=str(archive_dest),
            get_key=False,
        )
        mock_get_key.assert_not_called()


if __name__ == "__main__":
    unittest.main()


