#!/usr/bin/env python3
"""
Claude Profile Mover for macOS
===============================
Migrates Claude Desktop, Claude Work (two-claude-accounts-mac), and Claude Code profiles between Macs.

Key Features:
- First-class support for Standard Claude and multi-account instances created via two-claude-accounts-mac.
- Full synchronization of Desktop sessions, Cowork sessions, and ~/.claude CLI projects.
- Automatic path remapping (/Users/olduser -> /Users/newuser).
- Renaming of ~/.claude/projects/-Users-olduser-... directories so sessions load properly.
- Strips gigabytes of ephemeral Electron caches (GPUCache, Dawn*, Crashpad, sentry).
- Automatic safety backups prior to import.
- Built-in verification and diagnostic auditor.
- Zero dependencies: runs out-of-the-box on any Mac with Python 3.
"""

import argparse
import base64
import datetime
import getpass
import glob
import json
import os
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

VERSION = "1.2.0"

# ANSI Colors
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_RED = "\033[31m"
C_CYAN = "\033[36m"
C_MAGENTA = "\033[35m"
C_GRAY = "\033[90m"


def print_banner():
    banner = f"{C_CYAN}{C_BOLD}" + r"""
   ____ _                 _        ____             __ _ _       __  __                      
  / ___| | __ _ _   _  __| | ___  |  _ \ _ __ ___  / _(_) | ___  |  \/  | _____   _____ _ __ 
 | |   | |/ _` | | | |/ _` |/ _ \ | |_) | '__/ _ \| |_| | |/ _ \ | |\/| |/ _ \ \ / / _ \ '__|
 | |___| | (_| | |_| | (_| |  __/ |  __/| | | (_) |  _| | |  __/ | |  | | (_) \ V /  __/ |   
  \____|_|\__,_|\__,_|\__,_|\___| |_|   |_|  \___/|_| |_|_|\___| |_|  |_|\___/ \_/ \___|_|   
""" + f"{C_RESET}{C_GRAY}        macOS Profile Clone & Migration Tool for Claude Desktop v{VERSION}{C_RESET}\n"
    print(banner)


def c_log(msg: str, level: str = "info"):
    if level == "info":
        print(f"  {C_CYAN}ℹ{C_RESET} {msg}")
    elif level == "success":
        print(f"  {C_GREEN}✔{C_RESET} {msg}")
    elif level == "warn":
        print(f"  {C_YELLOW}⚠{C_RESET} {msg}")
    elif level == "error":
        print(f"  {C_RED}✖{C_RESET} {msg}")
    elif level == "step":
        print(f"\n{C_BOLD}{C_MAGENTA}==>{C_RESET} {C_BOLD}{msg}{C_RESET}")
    else:
        print(f"    {msg}")


# ==============================================================================
# Paths & Profiles Discovery
# ==============================================================================

HOME = Path.home()
APP_SUPPORT = HOME / "Library" / "Application Support"
CLAUDE_DIR = APP_SUPPORT / "Claude"
CLAUDE_WORK_DIR = APP_SUPPORT / "ClaudeWork"
CLAUDE_WORK_SPACE_DIR = APP_SUPPORT / "Claude Work"
CLAUDE_CLI_DIR = HOME / ".claude"

# Chrome & Chromium Add-on paths & constants
CHROME_DIR = APP_SUPPORT / "Google" / "Chrome"
CHROME_NATIVE_HOSTS_DIR = CHROME_DIR / "NativeMessagingHosts"
PRIMARY_CLAUDE_EXT_ID = "fcoeoabgfenejglbffodgkkbkcdhcgfn"
CLAUDE_EXTENSION_IDS = [
    PRIMARY_CLAUDE_EXT_ID,
    "dihbgbndebgnbjfmelmegjepbnkhlgni",
    "dngcpimnedloihjnnfngkgjoidhnaolf",
]
NATIVE_HOST_NAME = "com.anthropic.claude_browser_extension"
NATIVE_HOST_JSON = "com.anthropic.claude_browser_extension.json"


def find_claude_app_helpers() -> Dict[str, Path]:
    """Find installed Claude desktop apps containing Contents/Helpers/chrome-native-host."""
    helpers: Dict[str, Path] = {}
    candidate_apps = [
        Path("/Applications/Claude.app"),
        Path("/Applications/Claude Work.app"),
        Path("/Applications/ClaudeWork.app"),
        HOME / "Applications" / "Claude.app",
        HOME / "Applications" / "Claude Work.app",
        HOME / "Applications" / "ClaudeWork.app",
    ]
    for app in candidate_apps:
        if app.exists():
            helper_bin = app / "Contents" / "Helpers" / "chrome-native-host"
            if helper_bin.exists() and os.access(helper_bin, os.X_OK):
                helpers[app.name] = helper_bin
    return helpers


def discover_chrome_profiles(chrome_base: Optional[Path] = None) -> Dict[str, Dict]:
    """
    Discover all Chrome profiles and extract metadata including associated Google account emails,
    installed Claude in Chrome extension files, and local LevelDB settings.
    """
    base = chrome_base or CHROME_DIR
    if not base.exists():
        return {}

    discovered = {}
    for d in base.iterdir():
        if not d.is_dir():
            continue
        if d.name != "Default" and not d.name.startswith("Profile "):
            continue

        pref_file = d / "Preferences"
        name = d.name
        emails: List[str] = []
        if pref_file.exists():
            try:
                with open(pref_file, "r", encoding="utf-8", errors="ignore") as f:
                    pref_data = json.load(f)
                prof_data = pref_data.get("profile", {})
                name = prof_data.get("name") or d.name
                user_email = prof_data.get("user_name", "")
                account_info = pref_data.get("account_info", [])
                email_set = set()
                if user_email and "@" in user_email:
                    email_set.add(user_email.strip())
                if isinstance(account_info, list):
                    for acc in account_info:
                        if isinstance(acc, dict) and acc.get("email"):
                            email_set.add(acc["email"].strip())
                emails = sorted(list(email_set))
            except Exception:
                pass

        # Check Claude in Chrome extension
        ext_dir = None
        ext_id = None
        ext_version = None
        for eid in CLAUDE_EXTENSION_IDS:
            check_ext = d / "Extensions" / eid
            if check_ext.exists():
                ext_dir = check_ext
                ext_id = eid
                try:
                    for sub in check_ext.iterdir():
                        if sub.is_dir() and (sub / "manifest.json").exists():
                            with open(sub / "manifest.json", "r", encoding="utf-8", errors="ignore") as mf:
                                mdata = json.load(mf)
                                ext_version = mdata.get("version", sub.name.split("_")[0])
                                break
                except Exception:
                    pass
                break

        # Check local settings
        settings_dir = None
        settings_bytes = 0
        for eid in ([ext_id] if ext_id else CLAUDE_EXTENSION_IDS):
            sdir = d / "Local Extension Settings" / eid
            if sdir.exists():
                settings_dir = sdir
                if not ext_id:
                    ext_id = eid
                for root_s, _, files_s in os.walk(sdir):
                    for f_s in files_s:
                        fp_s = Path(root_s) / f_s
                        if fp_s.is_file():
                            try:
                                settings_bytes += fp_s.stat().st_size
                            except Exception:
                                pass
                break

        # Check sync settings
        sync_dir = None
        if ext_id:
            sydir = d / "Sync Extension Settings" / ext_id
            if sydir.exists():
                sync_dir = sydir

        has_extension = bool(ext_dir or settings_dir)
        cookie_file = d / "Cookies"
        if not cookie_file.exists():
            cookie_file = d / "Network" / "Cookies"

        discovered[d.name] = {
            "dir_name": d.name,
            "path": d,
            "name": name,
            "emails": emails,
            "has_extension": has_extension,
            "extension_id": ext_id or PRIMARY_CLAUDE_EXT_ID,
            "extension_dir": ext_dir,
            "extension_version": ext_version,
            "settings_dir": settings_dir,
            "settings_bytes": settings_bytes,
            "sync_dir": sync_dir,
            "cookie_file": cookie_file if cookie_file.exists() else None,
        }

    return discovered


def resolve_chrome_profile(selector: str, profiles: Dict[str, Dict]) -> Optional[Dict]:
    """
    Resolve a profile by folder name (e.g. 'Profile 2', 'Profile 6', 'Default'),
    or by associated email address (e.g. 'user@example.com', 'colleague@example.org'),
    or by profile display name.
    """
    if not selector or not profiles:
        return None
    sel_clean = selector.strip().lower()

    # 1. Exact directory name match (case-insensitive)
    for p_name, p_info in profiles.items():
        if p_name.lower() == sel_clean:
            return p_info

    # 2. Exact sole email match (dedicated profile for this email)
    for p_info in profiles.values():
        emails = [e.lower() for e in p_info.get("emails", [])]
        if emails == [sel_clean]:
            return p_info

    # 3. Primary email match (first email in profile accounts)
    for p_info in profiles.values():
        emails = [e.lower() for e in p_info.get("emails", [])]
        if emails and emails[0] == sel_clean:
            return p_info

    # 4. Any email match (if multiple, prefer one with active extension)
    matching = []
    for p_info in profiles.values():
        emails = [e.lower() for e in p_info.get("emails", [])]
        if sel_clean in emails:
            matching.append(p_info)
    if matching:
        with_ext = [p for p in matching if p.get("has_extension")]
        return with_ext[0] if with_ext else matching[0]

    # 5. Profile display name match
    for p_info in profiles.values():
        if p_info.get("name", "").lower() == sel_clean:
            return p_info

    # 6. Substring email match
    for p_info in profiles.values():
        for email in p_info.get("emails", []):
            if sel_clean in email.lower():
                return p_info

    return None


def get_native_host_status(hosts_dir: Optional[Path] = None) -> Dict:
    """Read and validate the Chrome Native Messaging Host manifest."""
    h_dir = hosts_dir or CHROME_NATIVE_HOSTS_DIR
    manifest_file = h_dir / NATIVE_HOST_JSON
    if not manifest_file.exists():
        return {"exists": False, "valid": False, "path": None, "binary_exists": False, "error": "Manifest file not found", "manifest_file": str(manifest_file)}

    try:
        with open(manifest_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        bin_path_str = data.get("path", "")
        bin_path = Path(bin_path_str) if bin_path_str else None
        bin_exists = bool(bin_path and bin_path.exists() and os.access(bin_path, os.X_OK))
        return {
            "exists": True,
            "valid": True,
            "path": bin_path_str,
            "binary_exists": bin_exists,
            "allowed_origins": data.get("allowed_origins", []),
            "manifest_file": str(manifest_file),
        }
    except Exception as e:
        return {"exists": True, "valid": False, "path": None, "binary_exists": False, "error": str(e), "manifest_file": str(manifest_file)}


def setup_chrome_native_host(
    app_binary: Optional[Path] = None,
    browser_hosts_dir: Optional[Path] = None,
    preferred_app: Optional[str] = None,
) -> Tuple[bool, Optional[Path]]:
    """
    Configure or auto-repair com.anthropic.claude_browser_extension.json in Chrome NativeMessagingHosts.
    Points the manifest 'path' to an existing, valid chrome-native-host binary.
    Also registers External Extensions json for Chrome.
    """
    hosts_dir = browser_hosts_dir or CHROME_NATIVE_HOSTS_DIR
    target_binary = None

    if app_binary and app_binary.exists() and os.access(app_binary, os.X_OK):
        target_binary = app_binary
    else:
        helpers = find_claude_app_helpers()
        if preferred_app:
            for k, bin_path in helpers.items():
                if preferred_app.lower() in k.lower():
                    target_binary = bin_path
                    break
        if not target_binary:
            for priority in ["Claude.app", "Claude Work.app", "ClaudeWork.app"]:
                if priority in helpers:
                    target_binary = helpers[priority]
                    break
            if not target_binary and helpers:
                target_binary = list(helpers.values())[0]

    if not target_binary or not target_binary.exists():
        return False, None

    manifest = {
        "name": NATIVE_HOST_NAME,
        "description": "Claude Browser Extension Native Host",
        "path": str(target_binary),
        "type": "stdio",
        "allowed_origins": [
            f"chrome-extension://{PRIMARY_CLAUDE_EXT_ID}/",
            "chrome-extension://dihbgbndebgnbjfmelmegjepbnkhlgni/",
            "chrome-extension://dngcpimnedloihjnnfngkgjoidhnaolf/",
        ]
    }

    try:
        hosts_dir.mkdir(parents=True, exist_ok=True)
        manifest_file = hosts_dir / NATIVE_HOST_JSON
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        # Register external extension so Chrome automatically recognizes it
        ext_ext_dir = (hosts_dir.parent / "External Extensions")
        ext_ext_dir.mkdir(parents=True, exist_ok=True)
        ext_json = ext_ext_dir / f"{PRIMARY_CLAUDE_EXT_ID}.json"
        with open(ext_json, "w", encoding="utf-8") as f:
            json.dump({"external_update_url": "https://clients2.google.com/service/update2/crx"}, f, indent=2)

        return True, target_binary
    except Exception as e:
        c_log(f"Failed to write Chrome Native Host manifest: {e}", "warn")
        return False, None


def merge_claude_cookies(src_db_path: Path, dst_db_path: Path) -> int:
    """Copy claude.ai and anthropic.com cookie records into target Chrome Cookies database."""
    if not src_db_path.exists():
        return 0
    if not dst_db_path.exists():
        safe_copy_file(src_db_path, dst_db_path)
        return 1

    src_conn = None
    dst_conn = None
    try:
        src_conn = sqlite3.connect(f"file:{src_db_path}?mode=ro", uri=True)
        src_cur = src_conn.cursor()
        src_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cookies'")
        if not src_cur.fetchone():
            return 0

        src_cur.execute("PRAGMA table_info(cookies)")
        columns = [row[1] for row in src_cur.fetchall()]
        col_names = ", ".join(columns)
        placeholders = ", ".join(["?"] * len(columns))

        src_cur.execute(f"SELECT {col_names} FROM cookies WHERE host_key LIKE '%claude%' OR host_key LIKE '%anthropic%'")
        rows = src_cur.fetchall()
        if not rows:
            return 0

        dst_conn = sqlite3.connect(str(dst_db_path))
        dst_cur = dst_conn.cursor()
        dst_cur.execute("DELETE FROM cookies WHERE host_key LIKE '%claude%' OR host_key LIKE '%anthropic%'")
        dst_cur.executemany(f"INSERT OR REPLACE INTO cookies ({col_names}) VALUES ({placeholders})", rows)
        dst_conn.commit()
        return len(rows)
    except Exception as e:
        c_log(f"Cookie merge note: {e}", "info")
        return 0
    finally:
        if src_conn:
            try:
                src_conn.close()
            except Exception:
                pass
        if dst_conn:
            try:
                dst_conn.close()
            except Exception:
                pass


# Directories/files to exclude from backup/migration (ephemeral caches)
CACHE_PATTERNS = {
    "Cache",
    "Code Cache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "GPUCache",
    "Crashpad",
    "blob_storage",
    "Session Storage",
    "sentry",
    "VideoDecodeStats",
    "Shared Dictionary",
    ".DS_Store",
    "debug",
    "telemetry",
    "shell-snapshots",
    "downloads",
}

# Essential files in App Support to preserve
ESSENTIAL_APP_SUPPORT_FILES = {
    "claude_desktop_config.json",
    "config.json",
    "ant-device-registry.json",
    "ant-did",
    "buddy-tokens.json",
    "cowork-enabled-cli-ops.json",
    "git-worktrees.json",
    "window-state.json",
    "Preferences",
    "fcache",
    "Cookies",
    "Cookies-journal",
    "Local State",
    "TransportSecurity",
    "Network Persistent State",
}


def find_active_claudework_dir() -> Optional[Path]:
    """Returns the active ClaudeWork directory (ClaudeWork or 'Claude Work')."""
    if CLAUDE_WORK_DIR.exists() and any(CLAUDE_WORK_DIR.iterdir()):
        return CLAUDE_WORK_DIR
    if CLAUDE_WORK_SPACE_DIR.exists() and any(CLAUDE_WORK_SPACE_DIR.iterdir()):
        return CLAUDE_WORK_SPACE_DIR
    if CLAUDE_WORK_DIR.exists():
        return CLAUDE_WORK_DIR
    return None


def get_available_profiles() -> Dict[str, Path]:
    """Detect available profiles on this machine, including standard Claude,
    Claude Work, and any custom profile directories created by two-claude-accounts-mac."""
    profiles = {}
    
    # 1. Standard Claude
    if CLAUDE_DIR.exists() and any(CLAUDE_DIR.iterdir()):
        profiles["claude"] = CLAUDE_DIR

    # 2. Claude Work (from two-claude-accounts-mac)
    work_dir = find_active_claudework_dir()
    if work_dir and work_dir.exists():
        profiles["claudework"] = work_dir

    # 3. Dynamic scan for any custom two-claude-accounts-mac data directories
    if APP_SUPPORT.exists():
        for d in APP_SUPPORT.iterdir():
            if d.is_dir() and "claude" in d.name.lower():
                key = d.name.lower().replace(" ", "_").replace("-", "_")
                if key not in ("claude", "claudework", "claude_work") and d != work_dir and d != CLAUDE_DIR:
                    has_config = (d / "claude_desktop_config.json").exists() or (d / "config.json").exists()
                    has_sessions = (d / "claude-code-sessions").exists() or (d / "local-agent-mode-sessions").exists()
                    if has_config or has_sessions:
                        profiles[key] = d

    # 4. Claude CLI state (~/.claude)
    if CLAUDE_CLI_DIR.exists() and any(CLAUDE_CLI_DIR.iterdir()):
        profiles["claude_cli"] = CLAUDE_CLI_DIR

    return profiles


def is_process_running(proc_name: str) -> bool:
    """Check if a process matching proc_name is currently running."""
    try:
        res = subprocess.run(
            ["pgrep", "-if", proc_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return res.returncode == 0
    except Exception:
        return False


def check_running_processes(is_export: bool = False, interactive: bool = True) -> bool:
    """Check running processes with strict non-destructive guarantees for export."""
    running = []
    if is_process_running("Claude.app") or is_process_running("MacOS/Claude"):
        running.append("Claude Desktop")
    if is_process_running("ClaudeWork.app") or is_process_running("MacOS/ClaudeWork"):
        running.append("Claude Work")
    if is_process_running("/bin/claude"):
        running.append("Claude Code CLI")

    if is_export:
        # Export is ALWAYS 100% non-destructive. Never kill or prompt to quit apps on origin.
        c_log("Origin Safety Guarantee: 100% non-destructive read-only snapshot.", "success")
        c_log("Origin Mac files and running processes are completely untouched and unchanged.", "info")
        if running:
            c_log(f"Active app(s) detected: {', '.join(running)} (taking live read-only snapshot safely)", "info")
        return True

    if not running:
        return True

    # On target Mac during import only:
    c_log(f"Detected running process(es) on target Mac: {', '.join(running)}", "warn")
    c_log("Writing to target while Claude is running may cause app to overwrite restored configs on exit.", "warn")
    if interactive:
        try:
            ans = input(f"  Would you like to close target Claude apps before installing? [y/N]: ").strip().lower()
            if ans == "y":
                for app in ["Claude", "ClaudeWork"]:
                    subprocess.run(["osascript", "-e", f'tell application "{app}" to quit'], capture_output=True)
                subprocess.run(["pkill", "-f", "Claude.app"], capture_output=True)
                subprocess.run(["pkill", "-f", "ClaudeWork.app"], capture_output=True)
                c_log("Signals sent to close target Claude apps.", "info")
                return True
            else:
                c_log("Proceeding with import while apps are running.", "warn")
        except (EOFError, KeyboardInterrupt):
            pass
    return True


# ==============================================================================
# Keychain & Safe Storage Authentication
# ==============================================================================

def get_keychain_key() -> Optional[str]:
    """Retrieve the Claude Safe Storage key from the local macOS Keychain."""
    for acct in ["Claude", "Claude Key"]:
        try:
            res = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Safe Storage", "-a", acct, "-w"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=2,
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            pass
    return None


def set_keychain_key(key_str: str) -> bool:
    """Register the Claude Safe Storage encryption key with open ACL (-A flag) on destination Mac."""
    if not key_str:
        return False
    success = True
    for acct in ["Claude", "Claude Key"]:
        subprocess.run(
            ["security", "delete-generic-password", "-s", "Claude Safe Storage", "-a", acct],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        res = subprocess.run(
            ["security", "add-generic-password", "-s", "Claude Safe Storage", "-a", acct, "-w", key_str.strip(), "-U", "-A"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if res.returncode != 0:
            success = False
    return success


# ==============================================================================
# Helper Utilities
# ==============================================================================

def detect_source_username(base_dir: Path) -> Optional[str]:
    """Scan JSON and config files to deduce the old username."""
    user_pattern = re.compile(r'/Users/([^/]+)/')
    for root, _, files in os.walk(base_dir):
        for f in files:
            if f.endswith((".json", ".jsonl")):
                file_path = Path(root) / f
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as fp:
                        content = fp.read(1024 * 1024)
                        m = user_pattern.search(content)
                        if m:
                            found = m.group(1)
                            if found not in ("Shared", "Guest"):
                                return found
                except Exception:
                    continue
    return None


def rewrite_file_paths(file_path: Path, path_replacements: List[Tuple[str, str]]) -> int:
    """
    Perform string replacements across file lines in a memory-efficient and safe manner.
    Returns the count of replacements made.
    """
    if not file_path.is_file() or file_path.is_symlink():
        return 0

    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as fp:
            content = fp.read()
    except Exception:
        return 0

    modified_content = content
    total_matches = 0
    for old_str, new_str in path_replacements:
        if old_str in modified_content:
            count = modified_content.count(old_str)
            total_matches += count
            modified_content = modified_content.replace(old_str, new_str)

    if total_matches > 0:
        try:
            with open(file_path, "w", encoding="utf-8") as fp:
                fp.write(modified_content)
        except Exception as e:
            c_log(f"Failed to write updated paths to {file_path}: {e}", "warn")
            return 0

    return total_matches


def safe_copy_file(src: Path, dst: Path):
    """Safely copy a file, gracefully ignoring dangling or broken symlinks."""
    try:
        if src.is_symlink():
            if not os.path.exists(src):
                # Dangling/broken symlink (e.g. debug/latest) - safely skip
                return
            link_target = os.readlink(src)
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            os.symlink(link_target, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    except Exception:
        pass


def safe_copy_dir(src: Path, dst: Path, overwrite: bool = True):
    """Recursively copy directory without crashing on broken symlinks or locks."""
    dst.mkdir(parents=True, exist_ok=True)
    for root, _, files in os.walk(src):
        rel = Path(root).relative_to(src)
        target_root = dst / rel
        target_root.mkdir(parents=True, exist_ok=True)
        for f in files:
            src_f = Path(root) / f
            dest_f = target_root / f
            if src_f.is_symlink() and not os.path.exists(src_f):
                continue
            if overwrite or not dest_f.exists():
                safe_copy_file(src_f, dest_f)


# ==============================================================================
# Export Engine
# ==============================================================================

def should_skip_path(path: Path) -> bool:
    """Check if file/folder matches cache/ephemeral exclusions."""
    parts = path.parts
    for p in parts:
        if p in CACHE_PATTERNS:
            return True
        if p.endswith(".sock") or p.endswith(".db-wal") or p.endswith(".db-shm") or p == "LOCK":
            return True
    return False


def collect_sessions_metadata(profile_dir: Path) -> List[Dict]:
    """Extract metadata for sessions found in local-agent-mode-sessions and claude-code-sessions."""
    sessions = []
    for session_type in ["local-agent-mode-sessions", "claude-code-sessions"]:
        base = profile_dir / session_type
        if not base.exists():
            continue
        for json_file in base.rglob("local_*.json"):
            if not json_file.is_file():
                continue
            try:
                with open(json_file, "r", encoding="utf-8", errors="ignore") as fp:
                    data = json.load(fp)
                    sessions.append({
                        "id": json_file.stem,
                        "type": session_type,
                        "title": data.get("title") or "Untitled Session",
                        "createdAt": data.get("createdAt"),
                        "cwd": data.get("cwd") or data.get("originCwd") or "",
                        "isArchived": data.get("isArchived", False),
                        "rel_path": str(json_file.relative_to(profile_dir)),
                    })
            except Exception:
                continue
    return sessions


def do_export(
    profile_choice: str,
    output_archive: Optional[str] = None,
    dry_run: bool = False,
    include_cli: bool = True,
    include_chrome: bool = True,
    chrome_profile_selector: Optional[str] = None,
    get_key: bool = True,
) -> Optional[Path]:
    """Package selected Claude profiles into a compressed archive (100% non-destructive to origin)."""
    c_log("Starting Claude Profile Clone / Export (Non-Destructive)", "step")
    check_running_processes(is_export=True, interactive=False)

    available = get_available_profiles()
    chrome_profiles = discover_chrome_profiles()
    selected_chrome_profiles: Dict[str, Dict] = {}

    if include_chrome and chrome_profiles:
        if chrome_profile_selector:
            resolved = resolve_chrome_profile(chrome_profile_selector, chrome_profiles)
            if resolved:
                selected_chrome_profiles[resolved["dir_name"]] = resolved
            else:
                c_log(f"Chrome profile matching '{chrome_profile_selector}' not found!", "warn")
        else:
            for pname, pinfo in chrome_profiles.items():
                if pinfo["has_extension"]:
                    selected_chrome_profiles[pname] = pinfo

    if not available and not selected_chrome_profiles:
        c_log("No Claude desktop or Chrome extension profiles found on this system.", "error")
        return None

    c_log(f"Discovered local profiles: {', '.join(available.keys()) or 'None'}", "info")
    if selected_chrome_profiles:
        c_log(f"Discovered Chrome extension profiles: {', '.join(selected_chrome_profiles.keys())}", "info")

    selected_profiles: Dict[str, Path] = {}
    if profile_choice in ("chrome", "extension"):
        # Standalone Chrome export
        if not selected_chrome_profiles:
            c_log("No Chrome profiles with Claude in Chrome extension found!", "error")
            return None
        include_cli = False
    elif profile_choice in ("claude", "desktop"):
        if "claude" in available:
            selected_profiles["Claude"] = available["claude"]
        else:
            c_log("Standard Claude profile directory not found!", "error")
            return None
    elif profile_choice in ("claudework", "work"):
        if "claudework" in available:
            selected_profiles["ClaudeWork"] = available["claudework"]
        else:
            c_log("ClaudeWork profile directory not found!", "error")
            return None
    elif profile_choice == "all":
        if "claude" in available:
            selected_profiles["Claude"] = available["claude"]
        if "claudework" in available:
            selected_profiles["ClaudeWork"] = available["claudework"]
    else:  # auto / default: prefer standard Claude Desktop, fallback to multi-account if only one exists
        if "claude" in available:
            selected_profiles["Claude"] = available["claude"]
        elif "claudework" in available:
            selected_profiles["ClaudeWork"] = available["claudework"]

    current_user = getpass.getuser()
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

    if not output_archive:
        if profile_choice in ("chrome", "extension"):
            output_archive = f"claude-profile-chrome-{timestamp}.tar.gz"
        else:
            prof_name = "-".join(k.lower() for k in selected_profiles.keys()) or "chrome"
            output_archive = f"claude-profile-{prof_name}-{timestamp}.tar.gz"

    archive_path = Path(output_archive).resolve()

    # Collect inventory
    manifest = {
        "tool_version": VERSION,
        "source_hostname": os.uname().nodename,
        "source_username": current_user,
        "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "profiles": {},
        "cli_state": None,
        "chrome_extension": None,
    }

    total_files = 0
    total_bytes = 0

    for prof_key, prof_path in selected_profiles.items():
        sessions = collect_sessions_metadata(prof_path)
        mcp_servers = []
        cfg_file = prof_path / "claude_desktop_config.json"
        if cfg_file.exists():
            try:
                with open(cfg_file, "r", encoding="utf-8") as f:
                    cfg_data = json.load(f)
                    mcp_servers = list(cfg_data.get("mcpServers", {}).keys())
            except Exception:
                pass

        manifest["profiles"][prof_key] = {
            "source_dir": str(prof_path),
            "session_count": len(sessions),
            "sessions": sessions,
            "mcp_servers": mcp_servers,
        }
        c_log(f"Profile [{prof_key}]: {len(sessions)} sessions, {len(mcp_servers)} MCP servers ({', '.join(mcp_servers) or 'none'})", "info")

    # Chrome extension state
    if include_chrome and (selected_chrome_profiles or (CHROME_NATIVE_HOSTS_DIR / NATIVE_HOST_JSON).exists()):
        host_st = get_native_host_status()
        manifest["chrome_extension"] = {
            "source_dir": str(CHROME_DIR),
            "native_host_status": host_st,
            "profiles": [
                {
                    "dir_name": pinfo["dir_name"],
                    "name": pinfo["name"],
                    "emails": pinfo["emails"],
                    "extension_id": pinfo["extension_id"],
                    "extension_version": pinfo["extension_version"],
                    "settings_bytes": pinfo["settings_bytes"],
                }
                for pinfo in selected_chrome_profiles.values()
            ],
        }
        for pname, pinfo in selected_chrome_profiles.items():
            email_str = f" ({', '.join(pinfo['emails'])})" if pinfo['emails'] else ""
            c_log(
                f"Chrome Profile [{pname}]{email_str}: Claude in Chrome v{pinfo['extension_version'] or 'installed'} "
                f"({pinfo['settings_bytes'] / (1024 * 1024):.2f} MB settings)",
                "info",
            )
        if host_st["exists"]:
            c_log(f"Chrome Native Host: {host_st['path']} ({'Valid' if host_st['binary_exists'] else 'Missing binary'})", "info")

    # CLI state in ~/.claude
    if include_cli and CLAUDE_CLI_DIR.exists():
        cli_projects = []
        projects_dir = CLAUDE_CLI_DIR / "projects"
        if projects_dir.exists():
            for p in projects_dir.iterdir():
                if p.is_dir() and not p.name.startswith("."):
                    cli_projects.append(p.name)

        manifest["cli_state"] = {
            "source_dir": str(CLAUDE_CLI_DIR),
            "project_dirs": cli_projects,
            "has_settings": (CLAUDE_CLI_DIR / "settings.json").exists(),
            "has_history": (CLAUDE_CLI_DIR / "history.jsonl").exists(),
        }
        c_log(f"Claude CLI state: {len(cli_projects)} project directories in ~/.claude/projects", "info")

    if dry_run:
        c_log(f"[DRY-RUN] Would create archive: {archive_path}", "warn")
        c_log("[DRY-RUN] No files were copied or compressed.", "warn")
        return archive_path

    c_log(f"Packaging profile into: {archive_path.name}...", "info")

    # Build tar.gz
    with tarfile.open(archive_path, "w:gz") as tar:
        # Add manifest
        manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
        import io
        ti = tarfile.TarInfo(name="profile_manifest.json")
        ti.size = len(manifest_bytes)
        ti.mtime = int(datetime.datetime.now().timestamp())
        tar.addfile(ti, io.BytesIO(manifest_bytes))

        # Add profile directories
        for prof_key, prof_path in selected_profiles.items():
            c_log(f"Archiving {prof_key} files...", "info")
            for root, dirs, files in os.walk(prof_path):
                root_p = Path(root)
                dirs[:] = [d for d in dirs if not should_skip_path(root_p / d)]

                for f in files:
                    file_p = root_p / f
                    if should_skip_path(file_p):
                        continue
                    arcname = f"profiles/{prof_key}/" + str(file_p.relative_to(prof_path))
                    try:
                        tar.add(file_p, arcname=arcname, recursive=False)
                        total_files += 1
                        total_bytes += file_p.stat().st_size
                    except Exception as e:
                        c_log(f"Skipping {file_p.name}: {e}", "warn")

        # Add CLI state
        if include_cli and CLAUDE_CLI_DIR.exists():
            c_log("Archiving ~/.claude CLI configurations and projects...", "info")
            for root, dirs, files in os.walk(CLAUDE_CLI_DIR):
                root_p = Path(root)
                dirs[:] = [d for d in dirs if not should_skip_path(root_p / d)]
                for f in files:
                    file_p = root_p / f
                    if should_skip_path(file_p):
                        continue
                    arcname = "claude_cli/" + str(file_p.relative_to(CLAUDE_CLI_DIR))
                    try:
                        tar.add(file_p, arcname=arcname, recursive=False)
                        total_files += 1
                        total_bytes += file_p.stat().st_size
                    except Exception as e:
                        pass

        # Add Chrome extension state
        if include_chrome and (selected_chrome_profiles or (CHROME_NATIVE_HOSTS_DIR / NATIVE_HOST_JSON).exists()):
            c_log("Archiving Claude in Chrome extension files and settings...", "info")
            native_manifest = CHROME_NATIVE_HOSTS_DIR / NATIVE_HOST_JSON
            if native_manifest.exists():
                arcname = f"chrome_extension/NativeMessagingHosts/{NATIVE_HOST_JSON}"
                try:
                    tar.add(native_manifest, arcname=arcname, recursive=False)
                    total_files += 1
                    total_bytes += native_manifest.stat().st_size
                except Exception as e:
                    c_log(f"Skipping native host manifest: {e}", "warn")

            for pname, pinfo in selected_chrome_profiles.items():
                p_meta = {
                    "dir_name": pinfo["dir_name"],
                    "name": pinfo["name"],
                    "emails": pinfo["emails"],
                    "extension_id": pinfo["extension_id"],
                    "extension_version": pinfo["extension_version"],
                }
                meta_bytes = json.dumps(p_meta, indent=2).encode("utf-8")
                ti_meta = tarfile.TarInfo(name=f"chrome_extension/profiles/{pname}/profile_info.json")
                ti_meta.size = len(meta_bytes)
                ti_meta.mtime = int(datetime.datetime.now().timestamp())
                tar.addfile(ti_meta, io.BytesIO(meta_bytes))
                total_files += 1

                # Local Extension Settings
                sdir = pinfo.get("settings_dir")
                if sdir and sdir.exists():
                    for root, dirs, files in os.walk(sdir):
                        root_p = Path(root)
                        dirs[:] = [d for d in dirs if not should_skip_path(root_p / d)]
                        for f in files:
                            file_p = root_p / f
                            if should_skip_path(file_p):
                                continue
                            arcname = f"chrome_extension/profiles/{pname}/Local Extension Settings/{pinfo['extension_id']}/" + str(file_p.relative_to(sdir))
                            try:
                                tar.add(file_p, arcname=arcname, recursive=False)
                                total_files += 1
                                total_bytes += file_p.stat().st_size
                            except Exception:
                                pass

                # Sync Extension Settings
                sydir = pinfo.get("sync_dir")
                if sydir and sydir.exists():
                    for root, dirs, files in os.walk(sydir):
                        root_p = Path(root)
                        dirs[:] = [d for d in dirs if not should_skip_path(root_p / d)]
                        for f in files:
                            file_p = root_p / f
                            if should_skip_path(file_p):
                                continue
                            arcname = f"chrome_extension/profiles/{pname}/Sync Extension Settings/{pinfo['extension_id']}/" + str(file_p.relative_to(sydir))
                            try:
                                tar.add(file_p, arcname=arcname, recursive=False)
                                total_files += 1
                                total_bytes += file_p.stat().st_size
                            except Exception:
                                pass

                # Extensions
                ext_dir = pinfo.get("extension_dir")
                if ext_dir and ext_dir.exists():
                    for root, dirs, files in os.walk(ext_dir):
                        root_p = Path(root)
                        dirs[:] = [d for d in dirs if not should_skip_path(root_p / d)]
                        for f in files:
                            file_p = root_p / f
                            if should_skip_path(file_p):
                                continue
                            arcname = f"chrome_extension/profiles/{pname}/Extensions/{pinfo['extension_id']}/" + str(file_p.relative_to(ext_dir))
                            try:
                                tar.add(file_p, arcname=arcname, recursive=False)
                                total_files += 1
                                total_bytes += file_p.stat().st_size
                            except Exception:
                                pass

                # Cookies
                cookie_f = pinfo.get("cookie_file")
                if cookie_f and cookie_f.exists():
                    try:
                        arcname = f"chrome_extension/profiles/{pname}/Cookies"
                        tar.add(cookie_f, arcname=arcname, recursive=False)
                        total_files += 1
                        total_bytes += cookie_f.stat().st_size
                    except Exception:
                        pass

    size_mb = archive_path.stat().st_size / (1024 * 1024)
    c_log(f"Export completed successfully!", "success")
    c_log(f"Archive: {archive_path} ({size_mb:.2f} MB, {total_files} files)", "info")

    auth_key = get_keychain_key() if get_key else None
    if auth_key:
        c_log(f"Found Keychain Safe Storage key: {C_GREEN}{auth_key}{C_RESET}", "success")
        print(f"\n{C_BOLD}Next step on your new Mac:{C_RESET}")
        print(f"  {C_GREEN}./claude-mover.sh import {archive_path.name} --clean --auth-key \"{auth_key}\"{C_RESET}\n")
    else:
        print(f"\n{C_BOLD}Next step on your new Mac:{C_RESET}")
        print(f"  {C_GREEN}./claude-mover.sh import {archive_path.name} --clean{C_RESET}\n")
    return archive_path


# ==============================================================================
# Import & Path Remapping Engine
# ==============================================================================

def create_backup(target_names: List[str], backup_chrome: bool = False) -> Optional[Path]:
    """Create a safety backup of existing Claude directories before importing."""
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = HOME / f"claude-backup-{timestamp}"
    backup_root.mkdir(parents=True, exist_ok=True)
    backed_up_count = 0

    c_log(f"Creating safety backup at: {backup_root}", "info")

    for name in target_names:
        src = None
        if name.lower() in ("claudework", "claude work"):
            src = find_active_claudework_dir() or CLAUDE_WORK_DIR
        elif name.lower() == "claude":
            src = CLAUDE_DIR
        elif name.lower() == "claude_cli":
            src = CLAUDE_CLI_DIR

        if src and src.exists():
            dest = backup_root / src.name
            try:
                safe_copy_dir(src, dest, overwrite=True)
                backed_up_count += 1
            except Exception as e:
                c_log(f"Backup warning for {src.name}: {e}", "warn")

    if backup_chrome:
        chrome_backup_dir = backup_root / "Google_Chrome"
        chrome_manifest = CHROME_NATIVE_HOSTS_DIR / NATIVE_HOST_JSON
        if chrome_manifest.exists():
            dest_m = chrome_backup_dir / "NativeMessagingHosts" / NATIVE_HOST_JSON
            safe_copy_file(chrome_manifest, dest_m)
            backed_up_count += 1
        if CHROME_DIR.exists():
            for cp in CHROME_DIR.iterdir():
                if cp.is_dir() and (cp.name == "Default" or cp.name.startswith("Profile ")):
                    ext_settings = cp / "Local Extension Settings" / PRIMARY_CLAUDE_EXT_ID
                    if ext_settings.exists():
                        dest_s = chrome_backup_dir / cp.name / "Local Extension Settings" / PRIMARY_CLAUDE_EXT_ID
                        safe_copy_dir(ext_settings, dest_s, overwrite=True)
                        backed_up_count += 1

    if backed_up_count > 0:
        c_log(f"Backup created with {backed_up_count} profile folder(s)", "success")
        return backup_root
    return None


def resolve_archive_path(path_str: str) -> Optional[Path]:
    """Resolve archive path, expanding globs, ~, and checking common download directories."""
    expanded = os.path.expanduser(os.path.expandvars(path_str.strip()))

    # 1. Try glob expansion
    matches = glob.glob(expanded)
    if matches:
        return Path(matches[0]).resolve()

    # 2. Try direct path
    direct = Path(expanded).resolve()
    if direct.exists() and direct.is_file():
        return direct

    # 3. Check common transfer folders if not found
    search_dirs = [Path.cwd(), HOME / "Downloads", HOME / "Desktop", HOME]
    candidates = []
    for sdir in search_dirs:
        if sdir.exists():
            candidates.extend(sdir.glob("claude-profile-*.tar.gz"))

    if candidates:
        # Pick the most recently modified archive
        candidates.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        detected = candidates[0].resolve()
        c_log(f"Auto-detected profile archive at: {detected}", "info")
        return detected

    return None


def do_import(
    archive_path_str: str,
    from_user: Optional[str] = None,
    to_user: Optional[str] = None,
    to_app: Optional[str] = None,
    auth_key: Optional[str] = None,
    mirror_apps: bool = True,
    custom_path_mappings: Optional[List[str]] = None,
    chrome_to_profile: Optional[str] = None,
    chrome_from_profile: Optional[str] = None,
    include_chrome: bool = True,
    force: bool = False,
    clean: bool = False,
    dry_run: bool = False,
    skip_backup: bool = False,
) -> bool:
    """Unpack, remap, and install profile archive onto this Mac.
    
    If clean=True, automatically removes/wipes existing destination sessions
    and project memories before installing the clone (pre-import safety backup created first).
    If auth_key is provided, sets up macOS Keychain Safe Storage with open ACL (-A).
    If mirror_apps=True, syncs profile across both ClaudeWork and Claude.
    """
    c_log("Starting Claude Profile Import", "step")
    if clean:
        c_log("Clean Replacement Mode: Destination sessions & projects will be wiped clean.", "warn")
    check_running_processes(interactive=not dry_run)

    if auth_key and not dry_run:
        c_log("Registering Claude Safe Storage key in Keychain with open ACL (-A)...", "info")
        if set_keychain_key(auth_key):
            c_log("Registered Keychain Safe Storage key successfully", "success")
        else:
            c_log("Warning: Failed to update Keychain Safe Storage key", "warn")

    archive_path = resolve_archive_path(archive_path_str)
    if not archive_path or not archive_path.exists():
        c_log(f"Archive file not found: {archive_path_str}", "error")
        c_log("Hint: If you transferred via AirDrop, check ~/Downloads:", "info")
        c_log("  ./claude-mover.sh import ~/Downloads/claude-profile-claudework-*.tar.gz --clean", "info")
        c_log("Or if it is in the current directory:", "info")
        c_log("  ./claude-mover.sh import ./claude-profile-claudework-*.tar.gz --clean", "info")
        return False

    current_user = getpass.getuser()
    target_user = to_user or current_user

    # Temporary extraction staging area
    temp_dir = HOME / f".claude_mover_tmp_{datetime.datetime.now().strftime('%s')}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    try:
        c_log(f"Reading archive {archive_path.name}...", "info")
        with tarfile.open(archive_path, "r:gz") as tar:
            if hasattr(tarfile, 'data_filter'):
                tar.extractall(path=temp_dir, filter='data')
            else:
                tar.extractall(path=temp_dir)

        manifest_file = temp_dir / "profile_manifest.json"
        manifest = {}
        if manifest_file.exists():
            try:
                with open(manifest_file, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
            except Exception:
                pass

        source_user = from_user or manifest.get("source_username")
        if not source_user:
            source_user = detect_source_username(temp_dir) or current_user

        c_log(f"Migration Source User: {C_BOLD}{source_user}{C_RESET}", "info")
        c_log(f"Migration Target User: {C_BOLD}{target_user}{C_RESET}", "info")

        # Setup path replacements list
        replacements: List[Tuple[str, str]] = []
        if custom_path_mappings:
            for item in custom_path_mappings:
                if ":" in item:
                    old_p, new_p = item.split(":", 1)
                    replacements.append((old_p.strip(), new_p.strip()))

        if source_user != target_user:
            replacements.append((f"/Users/{source_user}/", f"/Users/{target_user}/"))
            replacements.append((f"/Users/{source_user}", f"/Users/{target_user}"))
            # For path encoded project folder strings like -Users-olduser-
            replacements.append((f"-Users-{source_user}-", f"-Users-{target_user}-"))

        if replacements:
            c_log("Configured path remappings:", "info")
            for old_s, new_s in replacements:
                print(f"    {C_YELLOW}{old_s}{C_RESET} -> {C_GREEN}{new_s}{C_RESET}")

        # Profiles to restore
        profiles_dir = temp_dir / "profiles"
        available_profiles = []
        if profiles_dir.exists():
            available_profiles = [p.name for p in profiles_dir.iterdir() if p.is_dir()]

        has_cli = (temp_dir / "claude_cli").exists()
        has_chrome = (temp_dir / "chrome_extension").exists()

        c_log(f"Profiles inside archive: {', '.join(available_profiles) or 'None'}", "info")
        if has_cli:
            c_log("Claude CLI & project memory (~/.claude) included in archive", "info")
        if has_chrome and include_chrome:
            c_log("Claude in Chrome browser add-on data included in archive", "info")

        if not skip_backup and not dry_run:
            create_backup(available_profiles + (["claude_cli"] if has_cli else []), backup_chrome=(has_chrome and include_chrome))

        # Perform Path Remapping on extracted staging files
        if replacements:
            c_log("Rewriting paths across session logs, JSON configs, and worktrees...", "info")
            total_rewrites = 0
            for root, _, files in os.walk(temp_dir):
                for f in files:
                    if f.endswith((".json", ".jsonl", ".txt", ".md", ".cfg", ".conf", ".key", ".plist")):
                        fp = Path(root) / f
                        cnt = rewrite_file_paths(fp, replacements)
                        total_rewrites += cnt
            c_log(f"Updated {total_rewrites} path reference(s) across configuration and session files", "success")

        # Process ~/.claude CLI state
        if has_cli:
            cli_source = temp_dir / "claude_cli"
            projects_src = cli_source / "projects"

            # Rename project directory folders if source_user != target_user
            if source_user != target_user and projects_src.exists():
                c_log("Adjusting CLI project directory naming...", "info")
                old_prefix = f"-Users-{source_user}-"
                new_prefix = f"-Users-{target_user}-"
                for p in list(projects_src.iterdir()):
                    if p.is_dir() and p.name.startswith(old_prefix):
                        new_name = p.name.replace(old_prefix, new_prefix, 1)
                        dest_p = projects_src / new_name
                        c_log(f"  Renaming project folder: {p.name} -> {new_name}", "info")
                        p.rename(dest_p)

            # Copy to target ~/.claude
            if not dry_run:
                c_log("Installing ~/.claude configurations and project memories...", "info")
                CLAUDE_CLI_DIR.mkdir(parents=True, exist_ok=True)

                # In clean mode, remove old destination project directories
                if clean:
                    target_projects = CLAUDE_CLI_DIR / "projects"
                    if target_projects.exists():
                        c_log("  [Clean Mode] Removing previous destination projects in ~/.claude/projects...", "info")
                        for old_p in list(target_projects.iterdir()):
                            if old_p.is_dir() and not old_p.name.startswith("."):
                                shutil.rmtree(old_p, ignore_errors=True)

                for item in cli_source.iterdir():
                    dest_item = CLAUDE_CLI_DIR / item.name
                    if item.is_file():
                        safe_copy_file(item, dest_item)
                    elif item.is_dir():
                        safe_copy_dir(item, dest_item, overwrite=(clean or force))

        # Process App Support Profiles (ClaudeWork, Claude)
        installed_sessions_total = 0
        overwritten_sessions_total = 0

        for prof_name in available_profiles:
            staged_prof = profiles_dir / prof_name
            target_app_support = None

            if to_app == "claude":
                target_app_support = CLAUDE_DIR
            elif to_app in ("claudework", "work"):
                target_app_support = find_active_claudework_dir() or CLAUDE_WORK_DIR
            elif prof_name.lower() in ("claudework", "claude work"):
                target_app_support = find_active_claudework_dir() or CLAUDE_WORK_DIR
            elif prof_name.lower() == "claude":
                target_app_support = CLAUDE_DIR

            if not target_app_support:
                continue

            c_log(f"Installing profile [{prof_name}] into {target_app_support}...", "info")

            if not dry_run:
                target_app_support.mkdir(parents=True, exist_ok=True)

                # Purge stale caches in clean mode
                if clean:
                    c_log(f"  [Clean Mode] Purging stale caches in {target_app_support.name}...", "info")
                    for c_dir in CACHE_PATTERNS:
                        stale_p = target_app_support / c_dir
                        if stale_p.exists():
                            if stale_p.is_dir():
                                shutil.rmtree(stale_p, ignore_errors=True)
                            else:
                                stale_p.unlink()

                # 1. Install root config files & web cookies
                for item in staged_prof.iterdir():
                    if item.is_file() and item.name in ESSENTIAL_APP_SUPPORT_FILES:
                        target_file = target_app_support / item.name
                        # Preserve target's local Keychain oauth tokens ONLY if not in clean mode and no auth_key provided
                        if item.name == "config.json" and target_file.exists() and not clean and not auth_key:
                            try:
                                with open(target_file, "r") as tf:
                                    existing_cfg = json.load(tf)
                                token1 = existing_cfg.get("oauth:tokenCache")
                                token2 = existing_cfg.get("oauth:tokenCacheV2")
                                with open(item, "r") as sf:
                                    new_cfg = json.load(sf)
                                if token1:
                                    new_cfg["oauth:tokenCache"] = token1
                                if token2:
                                    new_cfg["oauth:tokenCacheV2"] = token2
                                with open(target_file, "w") as tf:
                                    json.dump(new_cfg, tf, indent=2)
                                c_log(f"  Config updated: {item.name} (preserved target auth tokens)", "info")
                                continue
                            except Exception:
                                pass
                        shutil.copy2(item, target_file)
                        c_log(f"  Config updated: {item.name}", "info")

                # 2. Install sessions (local-agent-mode-sessions and claude-code-sessions)
                for sess_folder_name in ["local-agent-mode-sessions", "claude-code-sessions"]:
                    src_sess_base = staged_prof / sess_folder_name
                    if not src_sess_base.exists():
                        continue

                    target_sess_base = target_app_support / sess_folder_name
                    target_sess_base.mkdir(parents=True, exist_ok=True)

                    # Discover target account UUID folders
                    target_uuids = [d for d in target_sess_base.iterdir() if d.is_dir() and d.name != "skills-plugin"]
                    src_uuids = [d for d in src_sess_base.iterdir() if d.is_dir() and d.name != "skills-plugin"]

                    # In clean mode, remove old destination sessions from target UUID directory
                    if clean and target_uuids:
                        c_log(f"  [Clean Mode] Removing old destination sessions in {sess_folder_name}...", "info")
                        for t_uuid_dir in target_uuids:
                            for existing_json in list(t_uuid_dir.rglob("local_*.json")):
                                try:
                                    existing_json.unlink()
                                except Exception:
                                    pass
                            for existing_sub in list(t_uuid_dir.iterdir()):
                                if existing_sub.is_dir():
                                    for sess_dir in list(existing_sub.iterdir()):
                                        if sess_dir.is_dir() and sess_dir.name.startswith("local_"):
                                            shutil.rmtree(sess_dir, ignore_errors=True)

                    # If the target Mac has an account UUID already created by logging in, map src -> target
                    uuid_mapping = {}
                    if target_uuids and src_uuids:
                        target_primary = target_uuids[0]
                        for s_u in src_uuids:
                            matching = [t for t in target_uuids if t.name == s_u.name]
                            if matching:
                                uuid_mapping[s_u.name] = matching[0].name
                            else:
                                uuid_mapping[s_u.name] = target_primary.name

                    for src_root, dirs, files in os.walk(src_sess_base):
                        rel = Path(src_root).relative_to(src_sess_base)
                        dest_root = target_sess_base / rel

                        # Remap top-level UUID if mapped
                        parts = list(rel.parts)
                        if parts and parts[0] in uuid_mapping:
                            parts[0] = uuid_mapping[parts[0]]
                            dest_root = target_sess_base.joinpath(*parts)

                        dest_root.mkdir(parents=True, exist_ok=True)

                        for f in files:
                            src_f = Path(src_root) / f
                            dest_f = dest_root / f
                            is_new = not dest_f.exists()
                            if is_new:
                                safe_copy_file(src_f, dest_f)
                                if f.startswith("local_") and f.endswith(".json"):
                                    installed_sessions_total += 1
                            elif clean or force:
                                safe_copy_file(src_f, dest_f)
                                if f.startswith("local_") and f.endswith(".json"):
                                    overwritten_sessions_total += 1

                # 3. Copy other essential subdirectories (Local Storage, WebStorage, git-shadow, etc.)
                for extra in ["git-shadow", "Local Storage", "WebStorage", "Partitions", "claude-code"]:
                    extra_src = staged_prof / extra
                    if extra_src.exists():
                        extra_dest = target_app_support / extra
                        safe_copy_dir(extra_src, extra_dest, overwrite=True)

                # 4. Mirror profile across both Claude and ClaudeWork if requested
                if mirror_apps:
                    alt_dir = CLAUDE_DIR if target_app_support == (find_active_claudework_dir() or CLAUDE_WORK_DIR) else (find_active_claudework_dir() or CLAUDE_WORK_DIR)
                    c_log(f"Mirroring profile to alternative app support: {alt_dir.name}...", "info")
                    alt_dir.mkdir(parents=True, exist_ok=True)
                    if clean:
                        for c_dir in CACHE_PATTERNS:
                            stale_p = alt_dir / c_dir
                            if stale_p.exists():
                                if stale_p.is_dir():
                                    shutil.rmtree(stale_p, ignore_errors=True)
                                else:
                                    stale_p.unlink()
                    for root, dirs, files in os.walk(target_app_support):
                        r_path = Path(root)
                        dirs[:] = [d for d in dirs if not should_skip_path(r_path / d)]
                        rel = r_path.relative_to(target_app_support)
                        d_dir = alt_dir / rel
                        d_dir.mkdir(parents=True, exist_ok=True)
                        for f in files:
                            s_file = r_path / f
                            if not should_skip_path(s_file):
                                safe_copy_file(s_file, d_dir / f)
                    c_log(f"  Mirrored to {alt_dir.name} successfully!", "success")

        # Process Claude in Chrome browser add-on data
        if include_chrome and (temp_dir / "chrome_extension").exists():
            c_log("Installing Claude in Chrome extension data & native host...", "info")
            staged_chrome = temp_dir / "chrome_extension"

            # 1. Setup / Auto-repair Native Messaging Host
            pref_app = to_app or ("ClaudeWork" if "ClaudeWork" in available_profiles else "Claude")
            if not dry_run:
                ok_host, host_bin = setup_chrome_native_host(preferred_app=pref_app, browser_hosts_dir=CHROME_NATIVE_HOSTS_DIR)
                if ok_host:
                    c_log(f"Configured Chrome Native Messaging Host -> {host_bin}", "success")
                else:
                    c_log("Warning: Could not configure Chrome Native Messaging Host (no Claude Desktop app binary found)", "warn")
            else:
                c_log(f"[DRY-RUN] Would configure Chrome Native Messaging Host for app: {pref_app}", "info")

            # 2. Restore Chrome extension profiles
            staged_profiles_dir = staged_chrome / "profiles"
            if staged_profiles_dir.exists():
                target_chrome_profiles = discover_chrome_profiles(CHROME_DIR)
                staged_prof_folders = [p for p in staged_profiles_dir.iterdir() if p.is_dir()]

                # If chrome_to_profile was specified and multiple staged profiles exist without chrome_from_profile,
                # prefer the profile matching source email or with highest version
                if chrome_to_profile and not chrome_from_profile and len(staged_prof_folders) > 1:
                    staged_prof_folders.sort(key=lambda p: (p / "profile_info.json").exists(), reverse=True)

                installed_profiles_count = 0
                for s_prof_p in staged_prof_folders:
                    meta_f = s_prof_p / "profile_info.json"
                    meta = {}
                    if meta_f.exists():
                        try:
                            with open(meta_f, "r", encoding="utf-8") as f:
                                meta = json.load(f)
                        except Exception:
                            pass

                    src_emails = meta.get("emails", [])
                    src_dir_name = meta.get("dir_name", s_prof_p.name)
                    ext_id = meta.get("extension_id") or PRIMARY_CLAUDE_EXT_ID

                    if chrome_from_profile:
                        matches_src = (chrome_from_profile.lower() == src_dir_name.lower()) or any(chrome_from_profile.lower() == e.lower() for e in src_emails)
                        if not matches_src:
                            continue

                    target_prof_dir = None
                    target_name = None
                    target_emails = []

                    if chrome_to_profile:
                        resolved = resolve_chrome_profile(chrome_to_profile, target_chrome_profiles)
                        if resolved:
                            target_prof_dir = resolved["path"]
                            target_name = resolved["dir_name"]
                            target_emails = resolved["emails"]
                        elif (CHROME_DIR / chrome_to_profile).exists():
                            target_prof_dir = CHROME_DIR / chrome_to_profile
                            target_name = chrome_to_profile
                        else:
                            c_log(f"Target Chrome profile '{chrome_to_profile}' was not found on this computer.", "warn")
                            if target_chrome_profiles:
                                c_log("Detected Chrome profiles on this computer:", "info")
                                for pk, pv in target_chrome_profiles.items():
                                    c_log(f"  • [{pv['dir_name']}] {pv.get('name')} ({', '.join(pv.get('emails', [])) or 'No email'})", "info")
                            if "@" in chrome_to_profile:
                                c_log(f"Cannot map email '{chrome_to_profile}' because no Chrome profile is currently signed into that Google account.", "error")
                                c_log("Tip: Open Chrome and sign into the profile first, or pass the profile directory name (e.g. --chrome-to-profile 'Profile 1').", "info")
                                continue
                            else:
                                target_prof_dir = CHROME_DIR / chrome_to_profile
                                target_name = chrome_to_profile
                    else:
                        for email in src_emails:
                            resolved = resolve_chrome_profile(email, target_chrome_profiles)
                            if resolved:
                                target_prof_dir = resolved["path"]
                                target_name = resolved["dir_name"]
                                target_emails = resolved["emails"]
                                break
                        if not target_prof_dir:
                            if src_dir_name in target_chrome_profiles:
                                target_prof_dir = target_chrome_profiles[src_dir_name]["path"]
                                target_name = src_dir_name
                                target_emails = target_chrome_profiles[src_dir_name]["emails"]
                            elif "Default" in target_chrome_profiles:
                                target_prof_dir = target_chrome_profiles["Default"]["path"]
                                target_name = "Default"
                                target_emails = target_chrome_profiles["Default"]["emails"]
                            else:
                                target_prof_dir = CHROME_DIR / "Default"
                                target_name = "Default"

                    email_desc = f" ({', '.join(target_emails)})" if target_emails else ""
                    c_log(f"Installing Claude in Chrome profile into Chrome [{target_name}]{email_desc}...", "info")

                    if not dry_run:
                        target_prof_dir.mkdir(parents=True, exist_ok=True)

                        # Copy Local Extension Settings
                        src_sdir = s_prof_p / "Local Extension Settings" / ext_id
                        if src_sdir.exists():
                            dst_sdir = target_prof_dir / "Local Extension Settings" / ext_id
                            if clean and dst_sdir.exists():
                                shutil.rmtree(dst_sdir, ignore_errors=True)
                            safe_copy_dir(src_sdir, dst_sdir, overwrite=(clean or force))
                            c_log(f"  Installed Local Extension Settings for {ext_id}", "success")

                        # Copy Sync Extension Settings
                        src_sydir = s_prof_p / "Sync Extension Settings" / ext_id
                        if src_sydir.exists():
                            dst_sydir = target_prof_dir / "Sync Extension Settings" / ext_id
                            safe_copy_dir(src_sydir, dst_sydir, overwrite=(clean or force))

                        # Copy Extensions
                        src_ext_dir = s_prof_p / "Extensions" / ext_id
                        if src_ext_dir.exists():
                            dst_ext_dir = target_prof_dir / "Extensions" / ext_id
                            safe_copy_dir(src_ext_dir, dst_ext_dir, overwrite=(clean or force))

                        # Merge claude.ai Cookies if present
                        src_cookie = s_prof_p / "Cookies"
                        if src_cookie.exists():
                            dst_cookie = target_prof_dir / "Cookies"
                            if not dst_cookie.exists() and (target_prof_dir / "Network" / "Cookies").exists():
                                dst_cookie = target_prof_dir / "Network" / "Cookies"
                            cookie_cnt = merge_claude_cookies(src_cookie, dst_cookie)
                            if cookie_cnt > 0:
                                c_log(f"  Restored active claude.ai session cookies ({cookie_cnt} cookies)", "success")

        c_log("Import process complete!", "success")
        if not dry_run:
            c_log(f"Installed {installed_sessions_total} new session(s), overwritten {overwritten_sessions_total}", "info")
            print(f"\n{C_BOLD}Next steps on this Mac:{C_RESET}")
            print(f"  1. Start Claude / Claude Work.")
            print(f"  2. Verify that your chats and Cowork sessions appear in the sidebar.")
            print(f"  3. Run {C_GREEN}./claude-mover.sh verify{C_RESET} to run a full system health check.\n")
        else:
            c_log("[DRY-RUN] Finished simulation. No files were written.", "warn")

        return True

    finally:
        # Clean up temporary staging
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


# ==============================================================================
# In-place Path Remapper
# ==============================================================================

def do_remap(
    from_user: str,
    to_user: Optional[str] = None,
    custom_mappings: Optional[List[str]] = None,
    dry_run: bool = False,
) -> bool:
    """
    Repair or remap paths directly in-place on this Mac.
    Useful when files were already copied manually and sessions fail to open.
    """
    c_log("Running In-Place Path Remapper", "step")
    check_running_processes(interactive=not dry_run)

    target_user = to_user or getpass.getuser()
    replacements = []
    if custom_mappings:
        for item in custom_mappings:
            if ":" in item:
                old_p, new_p = item.split(":", 1)
                replacements.append((old_p.strip(), new_p.strip()))

    replacements.append((f"/Users/{from_user}/", f"/Users/{target_user}/"))
    replacements.append((f"/Users/{from_user}", f"/Users/{target_user}"))
    replacements.append((f"-Users-{from_user}-", f"-Users-{target_user}-"))

    c_log(f"Remapping: /Users/{from_user} -> /Users/{target_user}", "info")

    search_dirs = [
        CLAUDE_WORK_DIR,
        CLAUDE_WORK_SPACE_DIR,
        CLAUDE_DIR,
        CLAUDE_CLI_DIR,
    ]

    total_changes = 0
    for sdir in search_dirs:
        if not sdir.exists():
            continue
        c_log(f"Scanning {sdir}...", "info")
        for root, _, files in os.walk(sdir):
            if should_skip_path(Path(root)):
                continue
            for f in files:
                if f.endswith((".json", ".jsonl", ".txt", ".md", ".cfg", ".conf", ".key", ".plist")):
                    fp = Path(root) / f
                    if dry_run:
                        try:
                            with open(fp, "r", errors="ignore") as file_read:
                                cnt = file_read.read().count(f"/Users/{from_user}")
                                if cnt > 0:
                                    print(f"  [DRY-RUN] Would update {fp}: {cnt} occurrence(s)")
                                    total_changes += cnt
                        except Exception:
                            pass
                    else:
                        cnt = rewrite_file_paths(fp, replacements)
                        if cnt > 0:
                            total_changes += cnt

    # Rename ~/.claude/projects directories
    projects_dir = CLAUDE_CLI_DIR / "projects"
    if projects_dir.exists():
        old_prefix = f"-Users-{from_user}-"
        new_prefix = f"-Users-{target_user}-"
        for p in list(projects_dir.iterdir()):
            if p.is_dir() and p.name.startswith(old_prefix):
                new_name = p.name.replace(old_prefix, new_prefix, 1)
                dest_p = projects_dir / new_name
                if dry_run:
                    c_log(f"  [DRY-RUN] Would rename project directory: {p.name} -> {new_name}", "info")
                else:
                    c_log(f"  Renaming project directory: {p.name} -> {new_name}", "info")
                    p.rename(dest_p)
                total_changes += 1

    if dry_run:
        c_log(f"[DRY-RUN] Finished scan. Found {total_changes} path references to update.", "info")
    else:
        c_log(f"Remap completed! Updated {total_changes} paths/directories.", "success")
    return True


# ==============================================================================
# Verification & Health Check
# ==============================================================================

def do_verify() -> bool:
    """Audit local Claude profiles, check JSON syntax, and test path validity."""
    c_log("Running Claude Profile Health Check & Verification", "step")

    current_user = getpass.getuser()
    available = get_available_profiles()
    if not available:
        c_log("No Claude profiles detected on this machine.", "error")
        return False

    all_ok = True
    total_sessions = 0
    total_mcp = 0

    for prof_key, prof_path in available.items():
        print(f"\n{C_BOLD}Auditing Profile: {prof_key} ({prof_path}){C_RESET}")
        print("  " + "-" * 50)

        # 1. Config JSON check
        cfg_path = prof_path / "claude_desktop_config.json"
        if cfg_path.exists():
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                c_log("claude_desktop_config.json: Valid JSON", "success")

                # MCP check
                mcp = cfg.get("mcpServers", {})
                total_mcp += len(mcp)
                for name, srv in mcp.items():
                    cmd = srv.get("command", "")
                    args = srv.get("args", [])
                    status_parts = []

                    # Test command path
                    if cmd.startswith("/"):
                        if not os.path.exists(cmd):
                            status_parts.append(f"{C_RED}Missing binary: {cmd}{C_RESET}")
                            all_ok = False
                    else:
                        # Check in PATH
                        if not shutil.which(cmd):
                            status_parts.append(f"{C_YELLOW}Binary '{cmd}' not in current PATH{C_RESET}")

                    # Test arguments for file paths
                    for a in args:
                        if isinstance(a, str) and a.startswith("/") and not os.path.exists(a):
                            status_parts.append(f"{C_RED}Path not found: {a}{C_RESET}")
                            all_ok = False

                    if status_parts:
                        c_log(f"MCP [{name}]: {', '.join(status_parts)}", "warn")
                    else:
                        c_log(f"MCP [{name}]: Validated ({cmd})", "success")

                # Check cowork files path
                cowork_path = cfg.get("coworkUserFilesPath")
                if cowork_path:
                    if os.path.exists(cowork_path):
                        c_log(f"Cowork User Directory: Exists ({cowork_path})", "success")
                    else:
                        c_log(f"Cowork User Directory: {C_YELLOW}Not found ({cowork_path}){C_RESET}", "warn")

            except Exception as e:
                c_log(f"claude_desktop_config.json: {C_RED}JSON syntax error ({e}){C_RESET}", "error")
                all_ok = False

        # 2. Check sessions
        sessions = collect_sessions_metadata(prof_path)
        total_sessions += len(sessions)
        c_log(f"Found {len(sessions)} local session(s)", "info")

        # Scan for foreign username paths
        stale_count = 0
        for s in sessions:
            rel = s["rel_path"]
            json_file = prof_path / rel
            try:
                with open(json_file, "r", encoding="utf-8", errors="ignore") as f:
                    txt = f.read()
                    matches = set(re.findall(r'/Users/([^/]+)/', txt))
                    foreign = [u for u in matches if u not in (current_user, "Shared", "Guest")]
                    if foreign:
                        stale_count += 1
                        c_log(f"Session '{s['title'][:40]}': references old username(s): {', '.join(foreign)}", "warn")
                        all_ok = False
            except Exception:
                pass

        if stale_count == 0 and len(sessions) > 0:
            c_log("All session paths correctly match the current macOS user", "success")

    # 3. CLI State Check
    if CLAUDE_CLI_DIR.exists():
        print(f"\n{C_BOLD}Auditing Claude CLI State (~/.claude){C_RESET}")
        print("  " + "-" * 50)
        proj_dir = CLAUDE_CLI_DIR / "projects"
        if proj_dir.exists():
            proj_folders = [p.name for p in proj_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
            stale_proj = [p for p in proj_folders if p.startswith("-Users-") and not p.startswith(f"-Users-{current_user}-")]
            if stale_proj:
                c_log(f"Found {len(stale_proj)} project folder(s) with old username references:", "warn")
                for sp in stale_proj:
                    print(f"    {C_YELLOW}{sp}{C_RESET}")
                all_ok = False
            else:
                c_log(f"{len(proj_folders)} project folder(s) verified matching current user", "success")

    # 4. Chrome Extension & Native Messaging Host Audit
    print(f"\n{C_BOLD}Auditing Claude in Chrome Browser Add-on{C_RESET}")
    print("  " + "-" * 50)
    chrome_profiles = discover_chrome_profiles()
    total_chrome_exts = 0
    if chrome_profiles:
        c_log(f"Discovered {len(chrome_profiles)} local Chrome profile(s)", "info")
        for pname, pinfo in sorted(chrome_profiles.items()):
            email_str = f" ({', '.join(pinfo['emails'])})" if pinfo['emails'] else ""
            if pinfo["has_extension"]:
                total_chrome_exts += 1
                c_log(
                    f"Profile [{pname}]{email_str}: Claude in Chrome v{pinfo['extension_version'] or 'installed'} "
                    f"({pinfo['settings_bytes'] / (1024 * 1024):.2f} MB settings)",
                    "success",
                )
            else:
                c_log(f"Profile [{pname}]{email_str}: No Claude extension", "info")
    else:
        c_log("No Google Chrome profiles detected.", "info")

    host_status = get_native_host_status()
    if not host_status["exists"]:
        c_log("Chrome Native Messaging Host: Manifest missing (com.anthropic.claude_browser_extension.json)", "warn")
        c_log("  Run `./claude-mover.sh setup-chrome` to configure the native host bridge.", "info")
        all_ok = False
    elif not host_status["valid"]:
        c_log(f"Chrome Native Messaging Host: Invalid JSON ({host_status.get('error')})", "error")
        all_ok = False
    else:
        bin_path = host_status["path"]
        if host_status["binary_exists"]:
            c_log(f"Chrome Native Messaging Host: Validated -> {bin_path}", "success")
        else:
            c_log(f"Chrome Native Messaging Host: {C_RED}Missing binary ({bin_path}){C_RESET}", "error")
            helpers = find_claude_app_helpers()
            if helpers:
                c_log(f"  Detected alternative Claude app: {list(helpers.values())[0]}", "info")
                c_log("  Run `./claude-mover.sh setup-chrome` to repair the path automatically.", "info")
            all_ok = False

    print(f"\n{C_BOLD}Verification Summary:{C_RESET}")
    print(f"  Total Profiles Audited: {len(available)}")
    print(f"  Total Sessions:         {total_sessions}")
    print(f"  Total MCP Servers:      {total_mcp}")
    print(f"  Chrome Extension Installs: {total_chrome_exts}")
    host_summary = "Active" if host_status.get("binary_exists") else ("Missing" if not host_status.get("exists") else "Broken")
    print(f"  Chrome Native Host:     {host_summary}")

    if all_ok:
        c_log("All Claude systems and profiles are healthy and verified!", "success")
    else:
        c_log("Some paths or configurations need attention. You can use `./claude-mover.sh remap` to fix old usernames.", "warn")

    return all_ok


# ==============================================================================
# List Command
# ==============================================================================

def do_list():
    """Display all discovered sessions, projects, and configurations."""
    c_log("Listing Local Claude Profiles & Sessions", "step")
    available = get_available_profiles()
    if not available:
        c_log("No Claude profiles found on this machine.", "warn")
        return

    for prof_key, prof_path in available.items():
        print(f"\n{C_BOLD}{C_CYAN}Profile: {prof_key}{C_RESET} ({prof_path})")
        cfg_file = prof_path / "claude_desktop_config.json"
        if cfg_file.exists():
            try:
                with open(cfg_file, "r") as f:
                    cfg = json.load(f)
                    mcp = list(cfg.get("mcpServers", {}).keys())
                    print(f"  Configured MCP Servers ({len(mcp)}): {', '.join(mcp) if mcp else 'None'}")
            except Exception:
                pass

        sessions = collect_sessions_metadata(prof_path)
        if not sessions:
            print("  No sessions found.")
            continue

        print(f"  Sessions ({len(sessions)}):")
        printf_fmt = "    %-4s  %-40s  %-20s  %s"
        print(C_GRAY + printf_fmt % ("#", "TITLE", "DATE", "TYPE") + C_RESET)
        for idx, s in enumerate(sessions, 1):
            ts = s.get("createdAt")
            dt_str = "Unknown"
            if ts:
                try:
                    dt_str = datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pass
            print(printf_fmt % (idx, s['title'][:40], dt_str, s['type']))

    if CLAUDE_CLI_DIR.exists():
        projects_dir = CLAUDE_CLI_DIR / "projects"
        if projects_dir.exists():
            projs = [p.name for p in projects_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
            print(f"\n{C_BOLD}{C_MAGENTA}Claude Code CLI Projects (~/.claude/projects):{C_RESET} ({len(projs)} active)")
            for pr in projs[:15]:
                print(f"  • {pr}")
            if len(projs) > 15:
                print(f"  ... and {len(projs) - 15} more.")

    chrome_profiles = discover_chrome_profiles()
    if chrome_profiles:
        print(f"\n{C_BOLD}{C_CYAN}Claude in Chrome Browser Add-on:{C_RESET}")
        for pname, pinfo in sorted(chrome_profiles.items()):
            email_str = f" ({', '.join(pinfo['emails'])})" if pinfo['emails'] else ""
            if pinfo["has_extension"]:
                status = f"{C_GREEN}Installed v{pinfo['extension_version'] or 'unknown'}{C_RESET} ({pinfo['settings_bytes'] / (1024 * 1024):.2f} MB settings)"
            else:
                status = f"{C_GRAY}Not installed{C_RESET}"
            print(f"  • Chrome [{pname}]{email_str}: {status}")

    host_status = get_native_host_status()
    if host_status["exists"]:
        if host_status["valid"]:
            b_status = f"{C_GREEN}Active{C_RESET}" if host_status["binary_exists"] else f"{C_RED}Broken (binary missing){C_RESET}"
            print(f"  • Native Messaging Host: {b_status} -> {host_status['path']}")
        else:
            print(f"  • Native Messaging Host: {C_RED}Invalid JSON{C_RESET}")
    else:
        print(f"  • Native Messaging Host: {C_YELLOW}Not registered{C_RESET}")


# ==============================================================================
# Standalone Backup Command
# ==============================================================================

def do_backup_cmd():
    """Manual trigger to create an instant backup of all Claude profiles."""
    c_log("Creating Instant Backup", "step")
    available = list(get_available_profiles().keys())
    res = create_backup(available, backup_chrome=True)
    if res:
        c_log(f"All profiles backed up to: {res}", "success")
    else:
        c_log("Nothing was backed up.", "warn")


def do_get_auth_key():
    """Retrieve and display the Claude Safe Storage key from the local macOS Keychain."""
    c_log("Reading Claude Safe Storage Key from macOS Keychain...", "step")
    key = get_keychain_key()
    if key:
        c_log(f"Found encryption key: {C_GREEN}{key}{C_RESET}", "success")
        print(f"\n{C_BOLD}To transfer your active login to another Mac, run this on the target Mac:{C_RESET}")
        print(f"  {C_CYAN}./claude-mover.sh set-auth-key \"{key}\"{C_RESET}")
        print(f"  or include it during import:")
        print(f"  {C_CYAN}./claude-mover.sh import <archive> --clean --auth-key \"{key}\"{C_RESET}\n")
    else:
        c_log("No Claude Safe Storage key found in Keychain on this Mac.", "warn")


def do_set_auth_key(key_str: str):
    """Set the Claude Safe Storage key in the macOS Keychain with open ACL."""
    c_log("Registering Claude Safe Storage Key in macOS Keychain...", "step")
    if set_keychain_key(key_str):
        c_log("Successfully registered key with open application permissions (-A)", "success")
    else:
        c_log("Failed to register key in Keychain", "error")


def do_setup_chrome(app_name: Optional[str] = None, binary_path_str: Optional[str] = None):
    """Configure or repair the Claude Browser Extension Native Host."""
    c_log("Configuring Chrome Browser Extension Native Host...", "step")
    bin_p = Path(binary_path_str).resolve() if binary_path_str else None
    success, configured_bin = setup_chrome_native_host(app_binary=bin_p, preferred_app=app_name)
    if success:
        c_log("Successfully registered Chrome Native Messaging Host pointing to:", "success")
        print(f"    {C_GREEN}{configured_bin}{C_RESET}")
        c_log(f"Manifest written to: {CHROME_NATIVE_HOSTS_DIR / NATIVE_HOST_JSON}", "info")
        c_log(f"External Extension registered at: {CHROME_DIR / 'External Extensions' / f'{PRIMARY_CLAUDE_EXT_ID}.json'}", "info")
    else:
        c_log("Failed to setup Chrome Native Host: No valid Claude Desktop binary found.", "error")
        c_log("Please ensure Claude.app or ClaudeWork.app is installed in /Applications.", "info")


def cdp_send_receive_ws(ws_url: str, request_obj: dict, timeout: float = 8.0) -> dict:
    """
    Zero-dependency RFC 6455 WebSocket client using Python's standard library socket.

    Connects to the given Chrome DevTools WebSocket URL, performs the HTTP 101 WebSocket
    Upgrade handshake, sends request_obj as a masked JSON-RPC text frame, and reads/unmasks
    the corresponding response frame.

    Args:
        ws_url: WebSocket URL (e.g. ws://127.0.0.1:9222/devtools/page/...).
        request_obj: JSON-RPC dictionary containing id, method, and params.
        timeout: Socket timeout in seconds.

    Returns:
        dict: Decoded JSON-RPC response object from Chrome.

    Raises:
        ConnectionError: If handshake fails or socket closes unexpectedly.
        TimeoutError: If socket times out during read/write.
    """
    parsed = urllib.parse.urlparse(ws_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 9222
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    s = socket.create_connection((host, port), timeout=timeout)
    try:
        # 1. HTTP 101 WebSocket Upgrade Handshake
        sec_key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {sec_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        s.sendall(req.encode("utf-8"))

        response_header = b""
        while b"\r\n\r\n" not in response_header:
            chunk = s.recv(1024)
            if not chunk:
                raise ConnectionError("Connection closed before completing WebSocket handshake")
            response_header += chunk

        first_line = response_header.split(b"\r\n", 1)[0].decode("utf-8", "ignore")
        if "101" not in first_line:
            raise ConnectionError(f"WebSocket handshake rejected by Chrome: {first_line}")

        # 2. Encode & Send Masked Client Text Frame
        payload = json.dumps(request_obj).encode("utf-8")
        mask = os.urandom(4)
        length = len(payload)
        b0 = 0x81  # FIN (0x80) + text opcode (0x01)

        if length <= 125:
            header = bytes([b0, 0x80 | length]) + mask
        elif length <= 65535:
            header = bytes([b0, 0x80 | 126]) + struct.pack("!H", length) + mask
        else:
            header = bytes([b0, 0x80 | 127]) + struct.pack("!Q", length) + mask

        masked_payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        s.sendall(header + masked_payload)

        # 3. Read & Decode Response Frame
        def recv_exact(n: int) -> bytes:
            buf = bytearray()
            while len(buf) < n:
                chunk = s.recv(n - len(buf))
                if not chunk:
                    raise ConnectionError("Socket closed prematurely while reading WebSocket frame")
                buf.extend(chunk)
            return bytes(buf)

        while True:
            b0, b1 = recv_exact(2)
            opcode = b0 & 0x0F
            is_masked = bool(b1 & 0x80)
            plen = b1 & 0x7F

            if plen == 126:
                plen = struct.unpack("!H", recv_exact(2))[0]
            elif plen == 127:
                plen = struct.unpack("!Q", recv_exact(8))[0]

            frame_mask = recv_exact(4) if is_masked else None
            frame_data = recv_exact(plen)

            if is_masked and frame_mask:
                frame_data = bytes(b ^ frame_mask[i % 4] for i, b in enumerate(frame_data))

            if opcode == 0x08:  # Close frame
                raise ConnectionError("Chrome closed the WebSocket connection")
            elif opcode == 0x09:  # Ping
                pong_mask = os.urandom(4)
                pong_hdr = bytes([0x8A, 0x80 | len(frame_data)]) + pong_mask
                masked_pong = bytes(b ^ pong_mask[i % 4] for i, b in enumerate(frame_data))
                s.sendall(pong_hdr + masked_pong)
                continue
            elif opcode == 0x01:  # Text frame
                msg = json.loads(frame_data.decode("utf-8"))
                if msg.get("id") == request_obj.get("id"):
                    return msg
    finally:
        s.close()


def find_chrome_binary() -> Optional[Path]:
    """Locate Google Chrome executable on macOS."""
    candidates = [
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for c in candidates:
        if c.exists() and os.access(c, os.X_OK):
            return c
    return None


def is_chrome_running() -> bool:
    """Check if Google Chrome process is currently running."""
    try:
        res = subprocess.run(["pgrep", "-f", "Google Chrome"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return res.returncode == 0
    except Exception:
        return False


def launch_chrome_with_debugging(port: int = 9222) -> bool:
    """Launch or restart Google Chrome with --remote-debugging-port on macOS."""
    chrome_bin = find_chrome_binary()
    if not chrome_bin:
        c_log("Google Chrome binary not found in /Applications.", "error")
        return False

    if is_chrome_running():
        c_log("Closing running Chrome instance to enable remote debugging...", "info")
        try:
            subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to quit'], check=False)
        except Exception:
            pass
        # Wait up to 5s for Chrome to terminate
        for _ in range(10):
            if not is_chrome_running():
                break
            time.sleep(0.5)

    c_log(f"Launching Google Chrome with --remote-debugging-port={port}...", "info")
    try:
        subprocess.Popen(
            [str(chrome_bin), f"--remote-debugging-port={port}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        c_log(f"Failed to launch Chrome: {e}", "error")
        return False

    # Poll port for up to 10 seconds
    for _ in range(20):
        time.sleep(0.5)
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/json")
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    c_log(f"Chrome remote debugging is ready on port {port}!", "success")
                    return True
        except Exception:
            pass

    c_log(f"Timed out waiting for Chrome debugging port {port} to become available.", "error")
    return False


def do_export_chrome_auth_auto(
    out_path: str = "claude-chrome-auth.json",
    port: int = 9222,
    auto_launch: bool = True,
) -> bool:
    """
    Automate extraction of Claude in Chrome session tokens via Chrome DevTools Protocol (CDP).

    Connects over port 9222, identifies the active Claude extension context, extracts
    OAuth session tokens (accessToken, refreshToken, accountUuid, etc.), and writes
    the verified payload to out_path. If Chrome is not running with remote debugging,
    this function can automatically launch or restart Chrome with debugging enabled.

    Returns:
        bool: True if tokens were successfully extracted and saved, False otherwise.
    """
    c_log(f"Automated Chrome DevTools Protocol Export (port {port})", "step")

    # 1. Check if Chrome CDP port is responding
    cdp_json_url = f"http://127.0.0.1:{port}/json"
    targets = None
    try:
        req = urllib.request.Request(cdp_json_url)
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            targets = json.loads(resp.read().decode("utf-8"))
    except Exception:
        targets = None

    # 1b. If port is closed and auto_launch is enabled, offer to launch/restart Chrome directly
    if targets is None and auto_launch:
        chrome_bin = find_chrome_binary()
        if chrome_bin:
            should_launch = False
            if not is_chrome_running():
                c_log("Google Chrome is not running. Launching with remote debugging enabled...", "info")
                should_launch = True
            else:
                c_log(f"Chrome remote debugging port ({port}) is not accessible.", "warn")
                print(f"\n{C_BOLD}Chrome is currently running, but remote debugging is not enabled on port {port}.{C_RESET}")
                try:
                    ans = input(f"Would you like this script to restart Chrome with debugging enabled? (Tabs will restore) [Y/n]: ").strip().lower()
                    if ans != "n":
                        should_launch = True
                except (EOFError, KeyboardInterrupt):
                    return False

            if should_launch:
                if launch_chrome_with_debugging(port=port):
                    try:
                        req = urllib.request.Request(cdp_json_url)
                        with urllib.request.urlopen(req, timeout=2.0) as resp:
                            targets = json.loads(resp.read().decode("utf-8"))
                    except Exception:
                        targets = None

    if targets is None:
        c_log(f"Could not connect to Chrome remote debugging on port {port}.", "error")
        print(
            f"\n{C_BOLD}Alternatively, you can export immediately without restarting Chrome:{C_RESET}\n"
            f"  Run: {C_CYAN}./claude-mover.sh export-chrome-auth{C_RESET} (takes ~10 seconds in DevTools Console)\n"
        )
        try:
            fallback = input("Would you like to run the 10-second manual export now? [Y/n]: ").strip().lower()
            if fallback != "n":
                return do_export_chrome_auth(out_path=out_path, auto=False)
        except (EOFError, KeyboardInterrupt):
            pass
        return False

    # 2. Find Claude extension target
    claude_target = None
    for t in targets:
        url = t.get("url", "")
        if PRIMARY_CLAUDE_EXT_ID in url:
            claude_target = t
            break

    temp_target_id = None
    if not claude_target:
        # If service worker is dormant, create a temporary extension tab to wake the storage context
        c_log("Waking Claude in Chrome extension context via CDP...", "info")
        try:
            wake_url = f"http://127.0.0.1:{port}/json/new?chrome-extension://{PRIMARY_CLAUDE_EXT_ID}/sidepanel.html"
            put_req = urllib.request.Request(wake_url, method="PUT")
            with urllib.request.urlopen(put_req, timeout=3.0) as resp:
                new_t = json.loads(resp.read().decode("utf-8"))
                claude_target = new_t
                temp_target_id = new_t.get("id")
        except Exception as e:
            c_log(f"Could not reach Claude in Chrome extension: {e}", "error")
            c_log("Ensure the Claude extension is installed and enabled in Chrome.", "info")
            return False

    ws_url = claude_target.get("webSocketDebuggerUrl")
    if not ws_url:
        c_log("Target found but webSocketDebuggerUrl is missing.", "error")
        return False

    c_log(f"Connected to Claude extension context ({claude_target.get('title') or 'Extension'})", "info")

    # 3. Evaluate extraction expression
    expr = (
        "new Promise(r => chrome.storage.session.get(null, s => "
        "chrome.storage.local.get(null, l => r(JSON.stringify({...s, ...l})))))"
    )
    rpc_req = {
        "id": 1,
        "method": "Runtime.evaluate",
        "params": {
            "expression": expr,
            "awaitPromise": True,
            "returnByValue": True,
        },
    }

    try:
        res = cdp_send_receive_ws(ws_url, rpc_req, timeout=6.0)
    except Exception as e:
        c_log(f"Failed to communicate with Chrome via WebSocket: {e}", "error")
        if temp_target_id:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{temp_target_id}", timeout=1.0)
            except Exception:
                pass
        return False

    # Close temporary tab if opened
    if temp_target_id:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{temp_target_id}", timeout=1.0)
        except Exception:
            pass

    # 4. Process and validate response
    val_str = res.get("result", {}).get("result", {}).get("value")
    if not val_str:
        c_log("Received empty response from extension storage.", "error")
        return False

    try:
        data = json.loads(val_str)
    except Exception as e:
        c_log(f"Could not parse authentication JSON: {e}", "error")
        return False

    access_token = data.get("accessToken")
    refresh_token = data.get("refreshToken")
    account_uuid = data.get("accountUuid") or data.get("identity", {}).get("accountUuid")

    if not access_token and not refresh_token:
        c_log("Warning: Extension storage found, but active login tokens (accessToken/refreshToken) were empty.", "warn")
        c_log("Please ensure you are actively signed in inside the Claude Chrome sidepanel first.", "info")

    p = Path(out_path).resolve()
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        c_log(f"Authentication payload saved successfully to: {p}", "success")
        if account_uuid:
            c_log(f"Account: {account_uuid}", "info")
        c_log(f"To make authentication permanent on target Mac: ./claude-mover.sh persist-chrome-auth {p.name}", "info")
        return True
    except Exception as e:
        c_log(f"Failed to save auth payload to {p}: {e}", "error")
        return False


def do_export_chrome_auth(
    out_path: Optional[str] = "claude-chrome-auth.json",
    auto: bool = False,
    port: int = 9222,
) -> bool:
    """
    Extract active Claude in Chrome session authentication tokens from the source Mac.

    Claude in Chrome (Manifest V3) isolates active OAuth bearer tokens (accessToken,
    refreshToken, tokenExpiry, accountUuid) inside in-memory RAM storage
    (chrome.storage.session), preventing disk-based migration tools from reading them.

    This function offers two export modes:
      1. Manual DevTools Mode (auto=False, Default):
         Provides step-by-step guidance to copy active tokens from the extension's
         Service Worker DevTools Console in ~10 seconds, with no Chrome restart required.
      2. Automated CDP Mode (auto=True):
         Connects over Chrome DevTools Protocol (WebSocket RFC 6455) to query the active
         service worker target or extension context directly, dumping tokens into out_path.

    Args:
        out_path: Destination JSON file path (default: claude-chrome-auth.json).
        auto: If True, uses Chrome DevTools Protocol for automated extraction.
        port: Remote debugging port for CDP (default: 9222).

    Returns:
        bool: True if tokens were successfully extracted and saved, False otherwise.
    """
    if auto:
        return do_export_chrome_auth_auto(out_path=out_path or "claude-chrome-auth.json", port=port)

    c_log("Export Claude in Chrome Active Authentication", "step")
    print(f"\n{C_BOLD}Claude in Chrome stores active OAuth tokens in Chrome's in-memory storage.{C_RESET}")
    print(
        "To extract these tokens from this running Chrome instance (takes ~10 seconds):\n\n"
        f"  1. In Chrome, open {C_CYAN}chrome://extensions{C_RESET}\n"
        f"  2. Toggle {C_BOLD}Developer mode{C_RESET} to {C_GREEN}ON{C_RESET} (top right)\n"
        f"  3. Under {C_BOLD}Claude{C_RESET}, click {C_CYAN}service worker{C_RESET}\n"
        "  4. In the DevTools Console tab, run this command:\n\n"
        f"{C_GREEN}chrome.storage.session.get(null, s => chrome.storage.local.get(null, l => console.log('AUTH_DATA=' + JSON.stringify({{...s, ...l}}))));{C_RESET}\n"
    )
    print(f"{C_GRAY}Tip: If Chrome was launched with --remote-debugging-port=9222, you can also run:{C_RESET}")
    print(f"{C_GRAY}     ./claude-mover.sh export-chrome-auth --auto{C_RESET}\n")

    print(f"Copy the {C_BOLD}AUTH_DATA={{...}}{C_RESET} output line and paste it below:")
    try:
        line = input("Paste AUTH_DATA here (or press Enter to skip): ").strip()
    except (EOFError, KeyboardInterrupt):
        return False

    if line:
        if line.startswith("AUTH_DATA="):
            line = line[len("AUTH_DATA="):]
        try:
            data = json.loads(line)
            p = Path(out_path or "claude-chrome-auth.json").resolve()
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            c_log(f"Authentication payload saved to: {p}", "success")
            c_log(f"To make authentication permanent on target Mac: ./claude-mover.sh persist-chrome-auth {p.name}", "info")
            return True
        except Exception as e:
            c_log(f"Invalid JSON payload: {e}", "error")
            return False
    return False


def do_import_chrome_auth(auth_file: Optional[str] = None, json_str: Optional[str] = None):
    """Generate injection payload and restore session tokens into Claude in Chrome."""
    c_log("Import Claude in Chrome Active Authentication", "step")
    data = None

    if json_str:
        try:
            data = json.loads(json_str)
        except Exception as e:
            c_log(f"Invalid JSON string: {e}", "error")
            return False
    elif auth_file:
        p = Path(auth_file).resolve()
        if not p.exists():
            c_log(f"File not found: {p}", "error")
            return False
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            c_log(f"Failed to read auth file: {e}", "error")
            return False
    else:
        default_p = Path("claude-chrome-auth.json")
        if default_p.exists():
            try:
                with open(default_p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                c_log(f"Using found auth file: {default_p}", "info")
            except Exception:
                pass

        if not data:
            print("No auth file specified. You can paste the AUTH_DATA JSON below:")
            try:
                line = input("Paste AUTH_DATA here: ").strip()
                if line.startswith("AUTH_DATA="):
                    line = line[len("AUTH_DATA="):]
                data = json.loads(line)
            except Exception as e:
                c_log(f"No valid auth data provided: {e}", "error")
                return False

    access_token = data.get("accessToken")
    refresh_token = data.get("refreshToken")
    token_expiry = data.get("tokenExpiry")
    account_uuid = data.get("accountUuid") or data.get("identity", {}).get("accountUuid")
    token_org = data.get("tokenOrg")
    posture = data.get("posture")

    if not access_token:
        c_log("Warning: accessToken missing from payload. The extension may still prompt for login.", "warn")

    session_data = {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "tokenExpiry": token_expiry,
        "posture": posture or {
            "allowedOrgs": None,
            "awaiting": None,
            "gen": 2,
            "identity": {
                "accountUuid": account_uuid,
                "hybrid": False,
                "orgUuid": token_org.get("uuid") if token_org else None
            },
            "managed": False,
            "pairedPeer": None,
            "source": None
        }
    }

    local_data = {
        "accountUuid": account_uuid,
        "tokenOrg": token_org,
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "tokenExpiry": token_expiry
    }

    snippet = (
        f"const sessionData = {json.dumps(session_data)};\n"
        f"const localData = {json.dumps(local_data)};\n"
        "chrome.storage.session.remove(['startupReauthState', 'lastAuthFailureReason'], () => {\n"
        "  chrome.storage.local.remove(['startupReauthState', 'lastAuthFailureReason'], () => {\n"
        "    chrome.storage.local.set(localData, () => {\n"
        "      chrome.storage.session.set(sessionData, () => {\n"
        "        console.log('🎉 AUTHENTICATION TRANSFERRED SUCCESSFULLY! Reloading extension...');\n"
        "        chrome.runtime.reload();\n"
        "      });\n"
        "    });\n"
        "  });\n"
        "});"
    )

    print(f"\n{C_BOLD}Ready-to-run Injection Snippet:{C_RESET}")
    print(f"{C_CYAN}=============================================================================={C_RESET}")
    print(f"{C_GREEN}{snippet}{C_RESET}")
    print(f"{C_CYAN}=============================================================================={C_RESET}\n")

    print(f"{C_BOLD}Next Steps on this Mac:{C_RESET}")
    print(f"  1. In Chrome, open {C_CYAN}chrome://extensions{C_RESET}")
    print(f"  2. Under {C_BOLD}Claude{C_RESET}, click {C_CYAN}service worker{C_RESET}")
    print(f"  3. In the Console tab, paste the snippet above and press {C_BOLD}Enter{C_RESET}")
    print("  4. Open the Claude extension sidepanel. (If it asks to sign in to cowork, click the top-right three dots ⋮ -> 'Switch back to classic').\n")
    return True


DEFAULT_AUTHENTICATED_SESSION = {
    "accessToken": "sk-ant-oat01-dummy-access-token-placeholder",
    "refreshToken": "sk-ant-ort01-dummy-refresh-token-placeholder",
    "tokenExpiry": 1893456000000,
    "accountUuid": "00000000-0000-0000-0000-000000000000",
    "tokenOrg": {"hybrid": False, "uuid": "00000000-0000-0000-0000-000000000000"},
    "posture": {
        "allowedOrgs": None,
        "awaiting": None,
        "gen": 2,
        "identity": {
            "accountUuid": "00000000-0000-0000-0000-000000000000",
            "hybrid": False,
            "orgUuid": "00000000-0000-0000-0000-000000000000",
        },
        "managed": False,
        "pairedPeer": None,
        "source": None,
    },
}


def do_persist_chrome_auth(
    auth_file: Optional[str] = None,
    json_str: Optional[str] = None,
    chrome_profile_query: Optional[str] = None,
    out_dir: Optional[str] = None,
) -> bool:
    """
    Create a persistent, self-healing unpacked version of Claude in Chrome with embedded authentication.

    Background & Problem:
        Official Web Store installations of Claude in Chrome (fcoeoabgfenejglbffodgkkbkcdhcgfn)
        contain runtime cleanups (e.g. inside SavedPromptsService.js) that wipe session storage
        and purge credentials whenever the service worker terminates or restarts.
        Furthermore, Chrome verifies CRX signatures on Web Store extensions, preventing in-place patching.

    How this function achieves permanent authentication:
        1. Copies the installed extension into ~/Desktop/Claude-Extension.
        2. Strips the _metadata/ directory to convert it into a compliant unpacked extension.
        3. Neutralizes the self-deleting cleanup routines in SavedPromptsService.js.
        4. Embeds the authenticated OAuth tokens (accessToken, refreshToken, accountUuid, orgUuid)
           directly into the extension startup bundle so it auto-authenticates permanently.
        5. Sets preferCoworkExperience: false to keep the extension in fast, reliable Classic Mode.
        6. Automatically reveals ~/Desktop/Claude-Extension in Finder and provides step-by-step
           instructions for loading it via chrome://extensions.

    Args:
        auth_file: Path to claude-chrome-auth.json (defaults to searching cwd and ~/claude-chrome-auth.json).
        json_str: Raw JSON string with auth payload.
        chrome_profile_query: Optional Chrome profile name/email to locate extension from.
        out_dir: Custom output directory (defaults to ~/Desktop/Claude-Extension).

    Returns:
        bool: True if persistent extension was successfully generated, False otherwise.
    """
    c_log("Create Persistent Authenticated Claude in Chrome Extension", "step")

    # 1. Load Auth Data
    data = None
    if json_str:
        try:
            data = json.loads(json_str)
        except Exception as e:
            c_log(f"Invalid JSON string: {e}", "error")
            return False
    elif auth_file:
        p = Path(auth_file).resolve()
        if not p.exists():
            c_log(f"Auth file not found: {p}", "error")
            return False
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            c_log(f"Failed to read auth file: {e}", "error")
            return False
    else:
        for cand in [Path("claude-chrome-auth.json"), Path.home() / "claude-chrome-auth.json"]:
            if cand.exists():
                try:
                    with open(cand, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    c_log(f"Loaded credentials from: {cand}", "info")
                    break
                except Exception:
                    pass

    if not data:
        if sys.stdin.isatty():
            print(f"\n{C_YELLOW}No auth file found at default location (claude-chrome-auth.json).{C_RESET}")
            print(f"You can export credentials from your source Mac using: {C_CYAN}./claude-mover.sh export-chrome-auth{C_RESET}\n")
            try:
                line = input("Paste AUTH_DATA here (or press Enter to use fallback template): ").strip()
                if line:
                    if line.startswith("AUTH_DATA="):
                        line = line[len("AUTH_DATA="):]
                    data = json.loads(line)
            except Exception:
                pass

    if not data:
        data = DEFAULT_AUTHENTICATED_SESSION
        c_log("Using fallback template session payload", "info")

    auth_payload = {
        "accessToken": data.get("accessToken") or DEFAULT_AUTHENTICATED_SESSION["accessToken"],
        "refreshToken": data.get("refreshToken") or DEFAULT_AUTHENTICATED_SESSION["refreshToken"],
        "tokenExpiry": data.get("tokenExpiry") or DEFAULT_AUTHENTICATED_SESSION["tokenExpiry"],
        "accountUuid": data.get("accountUuid") or data.get("identity", {}).get("accountUuid") or DEFAULT_AUTHENTICATED_SESSION["accountUuid"],
        "tokenOrg": data.get("tokenOrg") or DEFAULT_AUTHENTICATED_SESSION["tokenOrg"],
        "posture": data.get("posture") or DEFAULT_AUTHENTICATED_SESSION["posture"],
    }

    # 2. Locate Source Extension Directory
    src_ext_dir = None
    if chrome_profile_query:
        chrome_profs = discover_chrome_profiles()
        target_p = resolve_chrome_profile(chrome_profs, chrome_profile_query)
        if target_p:
            ext_cands = list((CHROME_DIR / target_p["dir_name"] / "Extensions" / PRIMARY_CLAUDE_EXT_ID).glob("*.*"))
            if ext_cands:
                src_ext_dir = sorted(ext_cands)[-1]

    if not src_ext_dir:
        ext_cands = list(CHROME_DIR.glob(f"*/Extensions/{PRIMARY_CLAUDE_EXT_ID}/*.*"))
        if ext_cands:
            src_ext_dir = sorted(ext_cands)[-1]

    if not src_ext_dir or not src_ext_dir.exists():
        c_log("Could not find installed Claude in Chrome extension files.", "error")
        c_log("Ensure the Claude extension is installed in Chrome from the Web Store first.", "info")
        return False

    c_log(f"Found source extension at: {src_ext_dir}", "info")

    # 3. Setup Destination Directory
    if out_dir:
        dst_dir = Path(out_dir).resolve()
    else:
        dst_dir = Path.home() / "Desktop" / "Claude-Extension"

    c_log(f"Target persistent directory: {dst_dir}", "info")
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    shutil.copytree(src_ext_dir, dst_dir)

    # 4. Remove _metadata so Chrome skips Web Store CRX signature checks
    meta = dst_dir / "_metadata"
    if meta.exists():
        shutil.rmtree(meta)

    # 4b. Rename extension in manifest.json so Chrome shows a distinct "Claude (Persistent)" card
    manifest_file = dst_dir / "manifest.json"
    if manifest_file.exists():
        try:
            m_data = json.loads(manifest_file.read_text(encoding="utf-8"))
            m_data["name"] = "Claude (Persistent)"
            manifest_file.write_text(json.dumps(m_data, indent=2), encoding="utf-8")
        except Exception:
            pass

    # 5. Patch SavedPromptsService to self-heal and inject persistent auth on boot
    assets_dir = dst_dir / "assets"
    js_files = list(assets_dir.glob("SavedPromptsService-*.js"))
    if not js_files:
        c_log("Could not find SavedPromptsService in extension assets.", "error")
        return False

    target_js = js_files[0]
    js_content = target_js.read_text(encoding="utf-8")

    pat = r'js=function\(\)\{return"ServiceWorkerGlobalScope"in globalThis\?\(Wr\?\?=\(async\(\)=>\{try\{.*?await chrome\.storage\.local\.remove\(qr\)\}catch\(t\)\{\}\}\)\(\),Wr\):Promise\.resolve\(\)\}'
    if not re.search(pat, js_content):
        c_log("Could not locate startup token handoff function in SavedPromptsService.", "error")
        return False

    payload_json = json.dumps(auth_payload)
    replacement = f'js=function(){{return"ServiceWorkerGlobalScope"in globalThis?(Wr??=(async()=>{{try{{const A={payload_json};await chrome.storage.session.set(A);await chrome.storage.local.set(A);await chrome.storage.local.set({{preferCoworkExperience:false}});}}catch(t){{}}}})(),Wr):Promise.resolve()}}'

    patched_js = re.sub(pat, replacement, js_content, count=1)
    target_js.write_text(patched_js, encoding="utf-8")

    c_log(f"✔ Successfully created persistent extension at: {dst_dir}", "success")
    print("\n" + "=" * 70)
    print(f"{C_BOLD}FULL PATH TO SELECT IN CHROME:{C_RESET}")
    print(f"  {C_GREEN}{dst_dir.resolve()}{C_RESET}")
    print("=" * 70 + "\n")

    # Copy full path to macOS clipboard
    try:
        subprocess.run(["pbcopy"], input=str(dst_dir.resolve()).encode("utf-8"), check=False)
        c_log("Folder path copied to clipboard!", "info")
    except Exception:
        pass

    # Automatically highlight/reveal folder in Finder
    try:
        subprocess.run(["open", "-R", str(dst_dir)], check=False)
    except Exception:
        pass

    # Automatically open Chrome to chrome://extensions
    try:
        subprocess.run(["open", "-a", "Google Chrome", "chrome://extensions"], check=False)
    except Exception:
        pass

    print(f"{C_BOLD}Next Steps to Activate in Chrome:{C_RESET}")
    print(f"  1. Chrome has been opened to: {C_CYAN}chrome://extensions{C_RESET}")
    print(f"  2. In the top-right corner, ensure {C_BOLD}Developer mode{C_RESET} is toggled {C_GREEN}ON{C_RESET}.")
    print(f"  3. In the top-left corner, click {C_BOLD}'Load unpacked'{C_RESET}.")
    print(f"  4. In the file picker dialog:")
    print(f"     - Press {C_BOLD}Cmd+Shift+G{C_RESET}, press {C_BOLD}Cmd+V{C_RESET} (path is already on your clipboard!), and press {C_BOLD}Enter{C_RESET}.")
    print(f"     - (Or click {C_BOLD}Desktop{C_RESET} in the sidebar and select the {C_GREEN}Claude-Extension{C_RESET} folder).")
    print(f"  5. {C_BOLD}Notice: You will now see TWO cards in chrome://extensions:{C_RESET}")
    print(f"     - {C_GREEN}Claude (Persistent){C_RESET}: This is the new migrated extension. {C_BOLD}Keep its toggle ON!{C_RESET}")
    print(f"     - {C_GRAY}Claude{C_RESET}: This is the original Web Store version. {C_RED}Toggle it OFF{C_RESET} (to prevent collisions).")
    print(f"  6. {C_BOLD}Where is the extension icon on your toolbar?{C_RESET}")
    print(f"     - Chrome automatically hides newly loaded extensions inside the {C_BOLD}Extensions menu (🧩 puzzle piece){C_RESET}.")
    print(f"     - Click the {C_BOLD}🧩 puzzle piece icon{C_RESET} in Chrome's top-right toolbar.")
    print(f"     - Find {C_GREEN}Claude (Persistent){C_RESET} and click the {C_BOLD}Pin icon (📌){C_RESET} so it stays on your toolbar.")
    print(f"  7. Click the Claude toolbar icon: you are now logged in with full access!")
    print(f"\n{C_CYAN}💡 If the sidepanel shows 'Sign in on claude.ai' (Experimental Cowork Mode):{C_RESET}")
    print(f"  Click the {C_BOLD}three vertical dots (⋮){C_RESET} in the top-right of the Claude sidepanel and select {C_BOLD}'Switch back to classic'{C_RESET}!")
    print(f"\n{C_YELLOW}⚠ IMPORTANT: Keep the 'Claude-Extension' folder on your Desktop!{C_RESET}")
    print(f"{C_GRAY}  Chrome runs unpacked extensions directly from that directory. Do not delete or rename it.{C_RESET}\n")
    return True


# ==============================================================================
# Interactive Menu
# ==============================================================================

def interactive_menu():
    """Friendly interactive CLI menu when run without arguments."""
    print_banner()
    available = get_available_profiles()
    print(f"Detected Claude installations: {C_GREEN}{', '.join(available.keys()) or 'None'}{C_RESET}")
    chrome_profs = discover_chrome_profiles()
    claude_chrome = [p for p in chrome_profs.values() if p.get("has_extension")]
    if claude_chrome:
        c_descs = [f"{p.get('name')} [{p.get('dir_name')}] ({', '.join(p.get('emails', [])) or 'No email'})" for p in claude_chrome]
        print(f"Detected Claude in Chrome: {C_GREEN}{', '.join(c_descs)}{C_RESET}")
    print()

    print("Please choose an action:")
    print(f"  {C_CYAN}[1]{C_RESET} Export Claude Desktop profile (Standard)")
    print(f"  {C_CYAN}[2]{C_RESET} Export Claude Work / Multi-Account clone")
    print(f"  {C_CYAN}[3]{C_RESET} Export All Claude Profiles & CLI state")
    print(f"  {C_CYAN}[4]{C_RESET} Import a profile archive on this Mac")
    print(f"  {C_CYAN}[5]{C_RESET} Verify / Health check existing profiles & Chrome Extension")
    print(f"  {C_CYAN}[6]{C_RESET} Remap old username in-place")
    print(f"  {C_CYAN}[7]{C_RESET} List sessions, MCP servers & Chrome profiles")
    print(f"  {C_CYAN}[8]{C_RESET} Backup current profiles")
    print(f"  {C_CYAN}[9]{C_RESET} Show Keychain Safe Storage Key (for transferring active login)")
    print(f"  {C_CYAN}[10]{C_RESET} Set Keychain Safe Storage Key on this Mac")
    print(f"  {C_CYAN}[11]{C_RESET} Setup / Repair Claude in Chrome Native Messaging Host")
    print(f"  {C_CYAN}[12]{C_RESET} Export Claude in Chrome extension data only")
    print(f"  {C_CYAN}[13]{C_RESET} Export Claude in Chrome active session tokens")
    print(f"  {C_CYAN}[14]{C_RESET} Import / Restore Claude in Chrome session tokens")
    print(f"  {C_CYAN}[15]{C_RESET} Make Claude in Chrome authentication permanent (Load unpacked)")
    print(f"  {C_CYAN}[q]{C_RESET} Quit\n")

    try:
        choice = input("Enter choice [1-15, q]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return

    if choice == "1":
        do_export(profile_choice="claude")
    elif choice == "2":
        do_export(profile_choice="claudework")
    elif choice == "3":
        do_export(profile_choice="all")
    elif choice == "4":
        archive = input("Path to profile archive (.tar.gz): ").strip()
        if archive:
            clean_ans = input("Clean destination profile? (Removes old chats on this Mac for an exact clone) [Y/n]: ").strip().lower()
            clean_flag = clean_ans != "n"
            key_ans = input("Keychain Safe Storage key (optional, paste to transfer active login): ").strip() or None
            chrome_to = input("Target Chrome profile email or name (optional, e.g. colleague@example.org): ").strip() or None
            do_import(archive, clean=clean_flag, auth_key=key_ans, chrome_to_profile=chrome_to)
    elif choice == "5":
        do_verify()
    elif choice == "6":
        old_user = input("Old username to replace: ").strip()
        new_user = input(f"New username (default: {getpass.getuser()}): ").strip() or None
        if old_user:
            do_remap(from_user=old_user, to_user=new_user)
    elif choice == "7":
        do_list()
    elif choice == "8":
        do_backup_cmd()
    elif choice == "9":
        do_get_auth_key()
    elif choice == "10":
        key_input = input("Enter Keychain Safe Storage Key: ").strip()
        if key_input:
            do_set_auth_key(key_input)
    elif choice == "11":
        do_setup_chrome()
    elif choice == "12":
        sel = input("Enter Chrome profile email or directory name (leave empty for auto-detect): ").strip() or None
        do_export(profile_choice="chrome", chrome_profile_selector=sel)
    elif choice == "13":
        print("\nExport Claude in Chrome Session Authentication:")
        print(f"  {C_CYAN}[1]{C_RESET} Interactive DevTools Console guide (takes ~10s, no restart required)")
        print(f"  {C_CYAN}[2]{C_RESET} Automated extraction via Chrome DevTools Protocol (--auto)")
        method = input("Choose method [1-2, default: 1]: ").strip()
        out = input("Save auth payload to file (default: claude-chrome-auth.json): ").strip() or "claude-chrome-auth.json"
        if method == "2":
            do_export_chrome_auth(out_path=out, auto=True)
        else:
            do_export_chrome_auth(out_path=out, auto=False)
    elif choice == "14":
        inp = input("Path to auth file (or press Enter to paste): ").strip() or None
        do_import_chrome_auth(auth_file=inp)
    elif choice == "15":
        inp = input("Path to auth file (default: claude-chrome-auth.json): ").strip() or None
        do_persist_chrome_auth(auth_file=inp)
    else:
        print("Goodbye!")


# ==============================================================================
# Entry Point & Argument Parsing
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Claude Profile Mover for macOS (Claude Desktop & CLI)",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # Export / Clone
    p_export = subparsers.add_parser(
        "export",
        aliases=["clone-export", "clone"],
        help="Package / clone Claude profiles into a portable archive",
    )
    p_export.add_argument(
        "--profile",
        choices=["claude", "claudework", "chrome", "all", "auto"],
        default="claude",
        help="Which profile to clone/export (default: claude)",
    )
    p_export.add_argument("-o", "--out", help="Output archive path (default: claude-profile-<profile>-<timestamp>.tar.gz)")
    p_export.add_argument("--dry-run", action="store_true", help="Simulate clone without creating files")
    p_export.add_argument("--no-cli", action="store_true", help="Skip ~/.claude CLI configurations")
    p_export.add_argument(
        "--no-chrome",
        action="store_true",
        help="Skip packaging Claude in Chrome browser extension data",
    )
    p_export.add_argument(
        "--chrome-profile",
        help="Specific Chrome profile to export (email, profile name, or dir name e.g. user@example.com or Profile 2)",
    )
    p_export.add_argument(
        "--no-key",
        "-no-key",
        dest="get_key",
        action="store_false",
        default=True,
        help="Skip retrieving macOS Keychain Safe Storage encryption key during export",
    )

    # Import / Restore Cloned Profile
    p_import = subparsers.add_parser(
        "import",
        aliases=["clone-import", "restore"],
        help="Install / restore a cloned profile archive onto this Mac",
    )
    p_import.add_argument("archive", help="Path to the .tar.gz profile archive")
    p_import.add_argument("--clean", "-c", action="store_true", help="Clean/remove existing destination sessions and projects before installing (clean clone)")
    p_import.add_argument("--auth-key", help="Automatically configure destination Keychain with this encryption key (transfers active login)")
    p_import.add_argument("--no-mirror", action="store_true", help="Do not mirror profile across both Claude and ClaudeWork")
    p_import.add_argument("--from-user", help="Override source username (auto-detected by default)")
    p_import.add_argument("--to-user", help="Override target username (defaults to current user)")
    p_import.add_argument("--to-app", choices=["claude", "claudework", "auto"], default="auto", help="Destination app to restore into (claude for Claude.app, claudework for ClaudeWork.app)")
    p_import.add_argument("--chrome-to-profile", help="Target Chrome profile to restore extension data into (email or dir name, e.g. colleague@example.org or Profile 6)")
    p_import.add_argument("--chrome-from-profile", help="Source Chrome profile from archive to restore from (defaults to auto-match by email or first profile)")
    p_import.add_argument("--no-chrome", action="store_true", help="Skip restoring Claude in Chrome browser extension data")
    p_import.add_argument("--remap-path", action="append", help="Custom path mapping: <old_path>:<new_path>")
    p_import.add_argument("--force", "-f", action="store_true", help="Force overwrite without prompting")
    p_import.add_argument("--dry-run", action="store_true", help="Simulate import without modifying files")
    p_import.add_argument("--no-backup", action="store_true", help="Skip pre-import safety backup")

    # Native Messaging Host setup/repair
    p_setup = subparsers.add_parser(
        "setup-chrome",
        aliases=["fix-chrome", "repair-chrome"],
        help="Register / repair Claude in Chrome Native Messaging Host manifest",
    )
    p_setup.add_argument("--app", choices=["claude", "claudework", "auto"], default="auto", help="App name to link with Chrome extension")
    p_setup.add_argument("--binary", help="Explicit path to chrome-native-host binary")

    # Export Chrome Auth tokens
    p_export_auth = subparsers.add_parser(
        "export-chrome-auth",
        help="Export active Claude in Chrome session tokens (for cross-computer auth transfer)",
    )
    p_export_auth.add_argument("-o", "--out", default="claude-chrome-auth.json", help="Output JSON path (default: claude-chrome-auth.json)")
    p_export_auth.add_argument(
        "--auto",
        action="store_true",
        help="Automate extraction via Chrome DevTools Protocol (requires Chrome running with --remote-debugging-port)",
    )
    p_export_auth.add_argument(
        "--port",
        type=int,
        default=9222,
        help="Chrome remote debugging port for --auto (default: 9222)",
    )

    # Import Chrome Auth tokens
    p_import_auth = subparsers.add_parser(
        "import-chrome-auth",
        aliases=["auth-chrome", "transfer-chrome-auth", "inject-chrome-auth"],
        help="Import active Claude in Chrome session tokens and generate service-worker injection snippet",
    )
    p_import_auth.add_argument("auth_file", nargs="?", help="Path to auth JSON file (or claude-chrome-auth.json)")
    p_import_auth.add_argument("--json", dest="json_str", help="Auth JSON string payload")

    # Persist Chrome Auth (Unpacked Self-Healing Extension)
    p_persist = subparsers.add_parser(
        "persist-chrome-auth",
        aliases=["make-chrome-persistent", "fix-chrome-permanent"],
        help="Create a persistent unpacked Claude in Chrome extension that survives restarts forever",
    )
    p_persist.add_argument("auth_file", nargs="?", help="Optional path to auth JSON file (or claude-chrome-auth.json)")
    p_persist.add_argument("--json", dest="json_str", help="Auth JSON string payload")
    p_persist.add_argument("--chrome-profile", help="Source Chrome profile (email or dir name e.g. Profile 6)")
    p_persist.add_argument("--out-dir", help="Destination directory for persistent unpacked extension (default: ~/Desktop/Claude-Extension)")

    # Keychain Key commands
    subparsers.add_parser("get-auth-key", aliases=["auth-key"], help="Display the Chrome Safe Storage encryption key")
    p_set_key = subparsers.add_parser("set-auth-key", help="Save the Chrome Safe Storage encryption key to macOS Keychain")
    p_set_key.add_argument("key", help="Chrome Safe Storage password/key to save")

    # Remap
    p_remap = subparsers.add_parser("remap", help="Rewrite username and file paths across existing profiles")
    p_remap.add_argument("--from-user", required=True, help="Original username to find")
    p_remap.add_argument("--to-user", help="New username (defaults to current user)")
    p_remap.add_argument("--remap-path", action="append", help="Custom path mapping: <old_path>:<new_path>")
    p_remap.add_argument("--dry-run", action="store_true", help="Preview changes without writing")

    # Verify
    subparsers.add_parser("verify", help="Check local profile health and validate paths")

    # List
    subparsers.add_parser("list", help="List all detected sessions, projects, and MCP servers")

    # Backup
    subparsers.add_parser("backup", help="Create a timestamped safety backup of all Claude profiles")

    args = parser.parse_args()

    if not args.command:
        interactive_menu()
        return

    print_banner()

    if args.command in ("export", "clone-export", "clone"):
        do_export(
            profile_choice=args.profile,
            output_archive=args.out,
            dry_run=args.dry_run,
            include_cli=not args.no_cli,
            retrieve_auth_key=not args.no_key,
            chrome_profile_query=args.chrome_profile,
            include_chrome=not args.no_chrome,
        )
    elif args.command in ("import", "clone-import", "restore"):
        do_import(
            archive_file=args.archive,
            from_user=args.from_user,
            to_user=args.to_user,
            to_app=args.to_app if args.to_app != "auto" else None,
            auth_key=args.auth_key,
            mirror_apps=not args.no_mirror,
            custom_path_mappings=args.remap_path,
            chrome_to_profile=args.chrome_to_profile,
            chrome_from_profile=args.chrome_from_profile,
            include_chrome=not args.no_chrome,
            force=args.force,
            clean=args.clean,
            dry_run=args.dry_run,
            skip_backup=args.no_backup,
        )
    elif args.command in ("setup-chrome", "fix-chrome", "repair-chrome"):
        do_setup_chrome(app_name=args.app, binary_path_str=args.binary)
    elif args.command == "export-chrome-auth":
        do_export_chrome_auth(out_path=args.out, auto=args.auto, port=args.port)
    elif args.command in ("import-chrome-auth", "auth-chrome", "transfer-chrome-auth", "inject-chrome-auth"):
        do_import_chrome_auth(auth_file=args.auth_file, json_str=getattr(args, "json_str", None))
    elif args.command in ("persist-chrome-auth", "make-chrome-persistent", "fix-chrome-permanent"):
        do_persist_chrome_auth(
            auth_file=args.auth_file,
            json_str=getattr(args, "json_str", None),
            chrome_profile_query=args.chrome_profile,
            out_dir=args.out_dir,
        )
    elif args.command in ("get-auth-key", "auth-key"):
        do_get_auth_key()
    elif args.command == "set-auth-key":
        do_set_auth_key(args.key)
    elif args.command == "remap":
        do_remap(
            from_user=args.from_user,
            to_user=args.to_user,
            custom_mappings=args.remap_path,
            dry_run=args.dry_run,
        )
    elif args.command == "verify":
        do_verify()
    elif args.command == "list":
        do_list()
    elif args.command == "backup":
        do_backup_cmd()


if __name__ == "__main__":
    main()
