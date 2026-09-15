#!/usr/bin/env python3
"""
Unit and Integration Tests for Claude Profile Mover / Cloner
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

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

        # Isolate Chrome directory so unit tests never inspect live Chrome data
        self.mock_chrome_dir = self.test_dir / "Google" / "Chrome"
        self.mock_chrome_hosts_dir = self.mock_chrome_dir / "NativeMessagingHosts"
        self.mock_chrome_dir.mkdir(parents=True, exist_ok=True)
        self.mock_chrome_hosts_dir.mkdir(parents=True, exist_ok=True)

        self.patch_chrome_dir = patch("claude_profile_mover.CHROME_DIR", self.mock_chrome_dir)
        self.patch_chrome_hosts = patch("claude_profile_mover.CHROME_NATIVE_HOSTS_DIR", self.mock_chrome_hosts_dir)
        self.patch_chrome_dir.start()
        self.patch_chrome_hosts.start()

    def tearDown(self):
        self.patch_chrome_hosts.stop()
        self.patch_chrome_dir.stop()
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

    def test_discover_chrome_profiles(self):
        """Test Chrome profile discovery and email account extraction."""
        mock_chrome = self.test_dir / "MockChrome"
        p2 = mock_chrome / "Profile 2"
        p6 = mock_chrome / "Profile 6"
        p2.mkdir(parents=True, exist_ok=True)
        p6.mkdir(parents=True, exist_ok=True)

        with open(p2 / "Preferences", "w") as f:
            json.dump({
                "profile": {"name": "Work Profile"},
                "account_info": [{"email": "user@example.com"}]
            }, f)

        with open(p6 / "Preferences", "w") as f:
            json.dump({
                "profile": {"name": "Secondary Profile"},
                "account_info": [{"email": "colleague@example.org"}]
            }, f)

        # Add extension files to Profile 2
        ext_dir = p2 / "Extensions" / cpm.PRIMARY_CLAUDE_EXT_ID / "1.0.93_0"
        ext_dir.mkdir(parents=True, exist_ok=True)
        with open(ext_dir / "manifest.json", "w") as f:
            json.dump({"version": "1.0.93"}, f)

        profiles = cpm.discover_chrome_profiles(mock_chrome)
        self.assertIn("Profile 2", profiles)
        self.assertIn("Profile 6", profiles)
        self.assertEqual(profiles["Profile 2"]["emails"], ["user@example.com"])
        self.assertTrue(profiles["Profile 2"]["has_extension"])
        self.assertEqual(profiles["Profile 2"]["extension_version"], "1.0.93")
        self.assertEqual(profiles["Profile 6"]["emails"], ["colleague@example.org"])
        self.assertFalse(profiles["Profile 6"]["has_extension"])

    def test_resolve_chrome_profile_different_target_number(self):
        """Verify profile resolution maps emails across different folder numbers on destination."""
        mock_profiles = {
            "Profile 2": {
                "dir_name": "Profile 2",
                "name": "Primary User",
                "emails": ["user@example.com"],
                "has_extension": True,
            },
            "Profile 15": {
                "dir_name": "Profile 15",
                "name": "Secondary User",
                "emails": ["colleague@example.org"],
                "has_extension": False,
            }
        }

        # Resolve by email to Profile 2
        res_src = cpm.resolve_chrome_profile("user@example.com", mock_profiles)
        self.assertIsNotNone(res_src)
        self.assertEqual(res_src["dir_name"], "Profile 2")

        # Resolve destination email to Profile 15 (different profile number!)
        res_dest = cpm.resolve_chrome_profile("colleague@example.org", mock_profiles)
        self.assertIsNotNone(res_dest)
        self.assertEqual(res_dest["dir_name"], "Profile 15")

        # Case-insensitive resolution
        res_case = cpm.resolve_chrome_profile("COLLEAGUE@EXAMPLE.ORG", mock_profiles)
        self.assertIsNotNone(res_case)
        self.assertEqual(res_case["dir_name"], "Profile 15")

        # Resolve by directory name directly
        res_dir = cpm.resolve_chrome_profile("Profile 15", mock_profiles)
        self.assertIsNotNone(res_dir)
        self.assertEqual(res_dir["dir_name"], "Profile 15")

    def test_setup_and_verify_chrome_native_host(self):
        """Test configuring and checking Native Messaging Host registration."""
        mock_hosts = self.test_dir / "NativeMessagingHosts"
        mock_hosts.mkdir(parents=True, exist_ok=True)

        mock_helper = self.test_dir / "chrome-native-host"
        mock_helper.write_text("#!/bin/sh\nexit 0\n")
        mock_helper.chmod(0o755)

        ok, configured_bin = cpm.setup_chrome_native_host(
            app_binary=mock_helper,
            browser_hosts_dir=mock_hosts
        )
        self.assertTrue(ok)
        self.assertEqual(configured_bin, mock_helper)

        manifest_file = mock_hosts / cpm.NATIVE_HOST_JSON
        self.assertTrue(manifest_file.exists())
        with open(manifest_file) as f:
            manifest_data = json.load(f)
        self.assertEqual(manifest_data["name"], cpm.NATIVE_HOST_NAME)
        self.assertEqual(manifest_data["path"], str(mock_helper))
        self.assertIn(f"chrome-extension://{cpm.PRIMARY_CLAUDE_EXT_ID}/", manifest_data["allowed_origins"])

        status = cpm.get_native_host_status(mock_hosts)
        self.assertTrue(status["exists"])
        self.assertTrue(status["valid"])
        self.assertTrue(status["binary_exists"])

    def test_merge_claude_cookies(self):
        """Test merging only claude.ai and anthropic.com cookies into destination database."""
        src_db = self.test_dir / "src_cookies.sqlite"
        dst_db = self.test_dir / "dst_cookies.sqlite"

        create_sql = """
        CREATE TABLE cookies (
            host_key TEXT NOT NULL,
            name TEXT NOT NULL,
            value TEXT NOT NULL,
            path TEXT NOT NULL,
            PRIMARY KEY (host_key, name, path)
        );
        """
        conn_src = sqlite3.connect(src_db)
        try:
            conn_src.execute(create_sql)
            conn_src.execute("INSERT INTO cookies VALUES ('.claude.ai', 'sessionKey', 'sk-ant-123', '/')")
            conn_src.execute("INSERT INTO cookies VALUES ('.anthropic.com', 'cf_clearance', 'clear-456', '/')")
            conn_src.execute("INSERT INTO cookies VALUES ('.google.com', 'SID', 'google-789', '/')")
            conn_src.commit()
        finally:
            conn_src.close()

        conn_dst = sqlite3.connect(dst_db)
        try:
            conn_dst.execute(create_sql)
            conn_dst.execute("INSERT INTO cookies VALUES ('.google.com', 'SID', 'target-google-existing', '/')")
            conn_dst.execute("INSERT INTO cookies VALUES ('.example.org', 'auth', 'target-auth-cookie', '/')")
            conn_dst.commit()
        finally:
            conn_dst.close()

        merged = cpm.merge_claude_cookies(src_db, dst_db)
        self.assertEqual(merged, 2)

        conn_check = sqlite3.connect(dst_db)
        try:
            rows = dict(conn_check.execute("SELECT host_key || ':' || name, value FROM cookies").fetchall())
        finally:
            conn_check.close()

        self.assertEqual(rows.get(".claude.ai:sessionKey"), "sk-ant-123")
        self.assertEqual(rows.get(".anthropic.com:cf_clearance"), "clear-456")
        # Ensure destination's existing cookies were preserved
        self.assertEqual(rows.get(".google.com:SID"), "target-google-existing")
        self.assertEqual(rows.get(".example.org:auth"), "target-auth-cookie")

    @patch("claude_profile_mover.find_claude_app_helpers")
    @patch("claude_profile_mover.check_running_processes", return_value=True)
    def test_chrome_extension_export_and_import_cross_profile(self, mock_proc, mock_helpers):
        """
        Full end-to-end test of exporting Claude in Chrome from Profile 2 (user@example.com)
        and restoring into Profile 14 (colleague@example.org) with a different profile number.
        """
        mock_src_chrome = self.test_dir / "SourceChrome"
        mock_dst_chrome = self.test_dir / "DestChrome"
        mock_src_hosts = mock_src_chrome / "NativeMessagingHosts"
        mock_dst_hosts = mock_dst_chrome / "NativeMessagingHosts"

        p2 = mock_src_chrome / "Profile 2"
        p2.mkdir(parents=True, exist_ok=True)
        mock_src_hosts.mkdir(parents=True, exist_ok=True)

        with open(p2 / "Preferences", "w") as f:
            json.dump({
                "profile": {"name": "Primary User"},
                "account_info": [{"email": "user@example.com"}]
            }, f)

        # Source extension files & settings
        src_ext = p2 / "Extensions" / cpm.PRIMARY_CLAUDE_EXT_ID / "1.0.93_0"
        src_ext.mkdir(parents=True, exist_ok=True)
        with open(src_ext / "manifest.json", "w") as f:
            json.dump({"version": "1.0.93"}, f)

        src_settings = p2 / "Local Extension Settings" / cpm.PRIMARY_CLAUDE_EXT_ID
        src_settings.mkdir(parents=True, exist_ok=True)
        with open(src_settings / "000003.log", "w") as f:
            f.write("claude_storage_leveldb_test_data")

        # Source host manifest
        with open(mock_src_hosts / cpm.NATIVE_HOST_JSON, "w") as f:
            json.dump({"name": cpm.NATIVE_HOST_NAME, "path": "/old/path"}, f)

        # Target machine: Colleague has Profile 14 (different number!)
        p14 = mock_dst_chrome / "Profile 14"
        p14.mkdir(parents=True, exist_ok=True)
        with open(p14 / "Preferences", "w") as f:
            json.dump({
                "profile": {"name": "Secondary User"},
                "account_info": [{"email": "colleague@example.org"}]
            }, f)

        # Target helper binary
        mock_helper = self.test_dir / "dest_app" / "Contents" / "Helpers" / "chrome-native-host"
        mock_helper.parent.mkdir(parents=True, exist_ok=True)
        mock_helper.write_text("#!/bin/sh\nexit 0\n")
        mock_helper.chmod(0o755)
        mock_helpers.return_value = {"ClaudeWork": mock_helper}

        # 1. Export from source by email
        archive_path = self.test_dir / "chrome_export.tar.gz"
        with patch.object(cpm, "CHROME_DIR", mock_src_chrome), \
             patch.object(cpm, "CHROME_NATIVE_HOSTS_DIR", mock_src_hosts):
            exp = cpm.do_export(
                profile_choice="chrome",
                output_archive=str(archive_path),
                chrome_profile_selector="user@example.com",
                dry_run=False,
                include_cli=False,
            )
            self.assertIsNotNone(exp)
            self.assertTrue(archive_path.exists())

        # 2. Import into target machine with --chrome-to-profile colleague@example.org
        with patch.object(cpm, "CHROME_DIR", mock_dst_chrome), \
             patch.object(cpm, "CHROME_NATIVE_HOSTS_DIR", mock_dst_hosts):
            ok = cpm.do_import(
                archive_path_str=str(archive_path),
                chrome_to_profile="colleague@example.org",
                clean=False,
                force=True,
                dry_run=False,
                skip_backup=True,
            )
            self.assertTrue(ok)

            # Check that settings were placed in Profile 14 (Colleague's profile)
            dst_settings_file = p14 / "Local Extension Settings" / cpm.PRIMARY_CLAUDE_EXT_ID / "000003.log"
            self.assertTrue(dst_settings_file.exists(), f"Expected {dst_settings_file} to exist")
            self.assertEqual(dst_settings_file.read_text(), "claude_storage_leveldb_test_data")

            # Check that Native Messaging Host manifest was installed and points to target helper
            dst_manifest_file = mock_dst_hosts / cpm.NATIVE_HOST_JSON
            self.assertTrue(dst_manifest_file.exists())
            with open(dst_manifest_file) as f:
                d_man = json.load(f)
            self.assertEqual(d_man["path"], str(mock_helper))

    def test_persist_chrome_auth(self):
        """Test creating a persistent unpacked Chrome extension with self-healing auth."""
        mock_chrome = self.test_dir / "MockChromePersist"
        mock_ext = mock_chrome / "Default" / "Extensions" / cpm.PRIMARY_CLAUDE_EXT_ID / "1.0.93_0"
        mock_assets = mock_ext / "assets"
        mock_assets.mkdir(parents=True, exist_ok=True)
        (mock_ext / "_metadata").mkdir(parents=True, exist_ok=True)
        (mock_ext / "_metadata" / "computed_hashes.json").write_text("{}")
        (mock_ext / "manifest.json").write_text('{"name": "Claude", "version": "1.0.93", "key": "testkey"}')

        sample_js = (
            'var Fr=["accessToken","refreshToken","tokenExpiry"],qr=[...Fr,"tokenHandOffAt"];'
            'js=function(){return"ServiceWorkerGlobalScope"in globalThis?(Wr??=(async()=>{try{const t=zr,'
            'e=await chrome.storage.local.get(Fr),n=Object.fromEntries(Object.entries(e).filter(([,t])=>null!=t));'
            'if(0===Object.keys(n).length)return;const r=await chrome.storage.session.get([Rn.ACCESS_TOKEN,Rn.REFRESH_TOKEN]);'
            'r[Rn.ACCESS_TOKEN]||r[Rn.REFRESH_TOKEN]||zr!==t||await chrome.storage.session.set(n),'
            'await chrome.storage.local.remove(qr)}catch(t){}})(),Wr):Promise.resolve()};'
        )
        (mock_assets / "SavedPromptsService-TEST.js").write_text(sample_js)

        out_persist = self.test_dir / "PersistentExtension"

        with patch.object(cpm, "CHROME_DIR", mock_chrome):
            ok = cpm.do_persist_chrome_auth(out_dir=str(out_persist))
            self.assertTrue(ok)

        self.assertTrue(out_persist.exists())
        self.assertFalse((out_persist / "_metadata").exists())
        patched_file = out_persist / "assets" / "SavedPromptsService-TEST.js"
        self.assertTrue(patched_file.exists())
        content = patched_file.read_text()
        self.assertIn("preferCoworkExperience:false", content)
        self.assertIn("sk-ant-oat01", content)
        self.assertNotIn("await chrome.storage.local.remove(qr)", content)
        m_out = json.loads((out_persist / "manifest.json").read_text())
        self.assertEqual(m_out.get("name"), "Claude (Persistent)")

    def test_export_chrome_auth_argparse_auto(self):
        """Test argument parsing for export-chrome-auth with --auto and custom port."""
        sys_argv = ["claude_profile_mover.py", "export-chrome-auth", "--auto", "--port", "9333", "-o", "my-auth.json"]
        with patch.object(sys, "argv", sys_argv):
            # Parse using parser inside main
            with patch("claude_profile_mover.do_export_chrome_auth") as mock_export:
                cpm.main()
                mock_export.assert_called_once_with(out_path="my-auth.json", auto=True, port=9333)

    def test_do_export_chrome_auth_auto_success(self):
        """Test automated export via Chrome DevTools Protocol with mocked CDP endpoints."""
        mock_targets = [
            {
                "id": "mock_worker_1",
                "type": "service_worker",
                "url": f"chrome-extension://{cpm.PRIMARY_CLAUDE_EXT_ID}/service-worker-loader.js",
                "title": "Claude Service Worker",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/mock_worker_1"
            }
        ]
        mock_eval_response = {
            "id": 1,
            "result": {
                "result": {
                    "type": "string",
                    "value": json.dumps({
                        "accessToken": "sk-ant-oat01-test-token",
                        "refreshToken": "sk-ant-ort01-test-token",
                        "accountUuid": "11111111-2222-3333-4444-555555555555",
                        "tokenExpiry": 1999999999999
                    })
                }
            }
        }

        out_file = self.test_dir / "auto-exported-auth.json"

        # Mock urllib.request.urlopen to return mock_targets JSON
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(mock_targets).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp), \
             patch("claude_profile_mover.cdp_send_receive_ws", return_value=mock_eval_response):
            ok = cpm.do_export_chrome_auth(out_path=str(out_file), auto=True, port=9222)
            self.assertTrue(ok)

        self.assertTrue(out_file.exists())
        saved_data = json.loads(out_file.read_text(encoding="utf-8"))
        self.assertEqual(saved_data.get("accessToken"), "sk-ant-oat01-test-token")
        self.assertEqual(saved_data.get("accountUuid"), "11111111-2222-3333-4444-555555555555")


if __name__ == "__main__":
    unittest.main()




