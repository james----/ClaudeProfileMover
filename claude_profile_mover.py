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
import datetime
import getpass
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

VERSION = "1.1.0"

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
        if p.endswith(".sock") or p.endswith(".db-wal") or p.endswith(".db-shm"):
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
    get_key: bool = True,
) -> Optional[Path]:
    """Package selected Claude profiles into a compressed archive (100% non-destructive to origin)."""
    c_log("Starting Claude Profile Clone / Export (Non-Destructive)", "step")
    check_running_processes(is_export=True, interactive=False)

    available = get_available_profiles()
    if not available:
        c_log("No Claude or Claude Work profiles found on this system.", "error")
        return None

    c_log(f"Discovered local profiles: {', '.join(available.keys())}", "info")

    selected_profiles: Dict[str, Path] = {}
    if profile_choice in ("claude", "desktop"):
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
        prof_name = "-".join(k.lower() for k in selected_profiles.keys())
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

def create_backup(target_names: List[str]) -> Optional[Path]:
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

        c_log(f"Profiles inside archive: {', '.join(available_profiles)}", "info")
        if has_cli:
            c_log("Claude CLI & project memory (~/.claude) included in archive", "info")

        if not skip_backup and not dry_run:
            create_backup(available_profiles + (["claude_cli"] if has_cli else []))

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

    print(f"\n{C_BOLD}Verification Summary:{C_RESET}")
    print(f"  Total Profiles Audited: {len(available)}")
    print(f"  Total Sessions:         {total_sessions}")
    print(f"  Total MCP Servers:      {total_mcp}")

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


# ==============================================================================
# Standalone Backup Command
# ==============================================================================

def do_backup_cmd():
    """Manual trigger to create an instant backup of all Claude profiles."""
    c_log("Creating Instant Backup", "step")
    available = list(get_available_profiles().keys())
    res = create_backup(available)
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


# ==============================================================================
# Interactive Menu
# ==============================================================================

def interactive_menu():
    """Friendly interactive CLI menu when run without arguments."""
    print_banner()
    available = get_available_profiles()
    print(f"Detected Claude installations: {C_GREEN}{', '.join(available.keys()) or 'None'}{C_RESET}\n")

    print("Please choose an action:")
    print(f"  {C_CYAN}[1]{C_RESET} Export Claude Desktop profile (Standard)")
    print(f"  {C_CYAN}[2]{C_RESET} Export Claude Work / Multi-Account clone")
    print(f"  {C_CYAN}[3]{C_RESET} Export All Claude Profiles & CLI state")
    print(f"  {C_CYAN}[4]{C_RESET} Import a profile archive on this Mac")
    print(f"  {C_CYAN}[5]{C_RESET} Verify / Health check existing profiles")
    print(f"  {C_CYAN}[6]{C_RESET} Remap old username in-place")
    print(f"  {C_CYAN}[7]{C_RESET} List sessions and MCP servers")
    print(f"  {C_CYAN}[8]{C_RESET} Backup current profiles")
    print(f"  {C_CYAN}[9]{C_RESET} Show Keychain Safe Storage Key (for transferring active login)")
    print(f"  {C_CYAN}[10]{C_RESET} Set Keychain Safe Storage Key on this Mac")
    print(f"  {C_CYAN}[q]{C_RESET} Quit\n")

    try:
        choice = input("Enter choice [1-10, q]: ").strip().lower()
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
            do_import(archive, clean=clean_flag, auth_key=key_ans)
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
        choices=["claude", "claudework", "all", "auto"],
        default="claude",
        help="Which profile to clone/export (default: claude)",
    )
    p_export.add_argument("-o", "--out", help="Output archive path (default: claude-profile-<profile>-<timestamp>.tar.gz)")
    p_export.add_argument("--dry-run", action="store_true", help="Simulate clone without creating files")
    p_export.add_argument("--no-cli", action="store_true", help="Skip ~/.claude CLI configurations")
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
    p_import.add_argument("--remap-path", action="append", help="Custom path mapping: <old_path>:<new_path>")
    p_import.add_argument("-f", "--force", action="store_true", help="Overwrite existing sessions")
    p_import.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    p_import.add_argument("--no-backup", action="store_true", help="Skip pre-migration safety backup")

    # Auth Key Commands
    subparsers.add_parser("get-auth-key", aliases=["auth-key"], help="Retrieve Claude Safe Storage encryption key from macOS Keychain")
    
    p_set_key = subparsers.add_parser("set-auth-key", help="Register Claude Safe Storage key into macOS Keychain with open ACL (-A)")
    p_set_key.add_argument("key", help="Base64 encryption key to register into Keychain")

    # Remap
    p_remap = subparsers.add_parser("remap", help="Fix username and paths in-place")
    p_remap.add_argument("--from-user", required=True, help="Old username to replace")
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
            get_key=args.get_key,
        )
    elif args.command in ("import", "clone-import", "restore"):
        do_import(
            archive_path_str=args.archive,
            from_user=args.from_user,
            to_user=args.to_user,
            to_app=args.to_app if args.to_app != "auto" else None,
            auth_key=args.auth_key,
            mirror_apps=not args.no_mirror,
            custom_path_mappings=args.remap_path,
            force=args.force,
            clean=args.clean,
            dry_run=args.dry_run,
            skip_backup=args.no_backup,
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
