# Claude Profile Mover / Cloner for macOS 🚀

A fast, reliable CLI tool to **clone and migrate** your **Claude Desktop** (`Claude.app`) and **Claude Code CLI** profiles from one Mac to another without broken paths, missing sessions, or blank sidebars.

It automatically handles macOS hardware Keychain encryption keys, wipes stale destination caches, rewrites hardcoded username paths across all session logs, and verifies setup health in seconds.

---

## The Problem: Why Manual Copying Fails

If you have ever tried copying `~/Library/Application Support/Claude` or `~/.claude` to a new Mac via Finder, AirDrop, Time Machine, or Migration Assistant, you likely encountered blank sidebars, missing chats, or broken MCP servers:

1. **Hardcoded User Paths**:
   - `claude_desktop_config.json` stores absolute paths to MCP server binaries and workspaces (`/Users/olduser/...`).
   - Cowork and Code tab sessions (`local_*.json`, `audit.jsonl`) store absolute paths to project directories.
2. **Path-Encoded CLI Project Memories**:
   - Claude Code stores project memory in `~/.claude/projects/` under folders named after the absolute path (e.g., `-Users-olduser-src-myproject`). If the username changes on the new Mac, Claude cannot link past chats to local repositories.
3. **macOS Hardware Keychain Binding (`Claude Safe Storage`)**:
   - Chromium encrypts OAuth session tokens (`config.json`) and web cookies (`Cookies` containing `sessionKeyV3`) using an AES-128 key derived from your Mac's Keychain (`"Claude Safe Storage"`).
   - Copying files to another Mac without transferring this key causes decryption failure, forcing you back to the login screen.
4. **Cache & Lock Pollution**:
   - Electron generates gigabytes of temporary runtime files (`GPUCache`, `DawnWebGPUCache`, `Crashpad`, `Session Storage`, SQLite lock files). Copying these causes permission conflicts and app freezes.

**Claude Profile Mover solves all of these automatically.** It packages only the essential configs, conversation logs, web tokens, and project memories, rewrites all paths to match your new macOS username, strips ephemeral caches, and migrates active Keychain login sessions.

---

## Quick Start: Migrating to a New Mac

Clone or copy **ClaudeProfileMover** to your Mac:
```bash
git clone https://github.com/james----/ClaudeProfileMover.git
cd ClaudeProfileMover
```

### Step 1: On Your Current Mac (Origin)

1. **Package your profile** (100% read-only snapshot; running apps remain open and untouched):

   - **Option A: Turnkey Active Login Transfer (Recommended - Zero Web Logins!)**
     ```bash
     cd ~/ClaudeProfileMover
     ./claude-mover.sh export
     ```
     *Creates a portable archive (`claude-profile-*.tar.gz`) and automatically retrieves your macOS Keychain Safe Storage key so you don't have to log in on the new Mac.*

   - **Option B: Standard Export (Manual Browser Login on New Mac)**
     ```bash
     cd ~/ClaudeProfileMover
     ./claude-mover.sh export --no-key
     ```
     *(Creates the archive without touching your macOS Keychain. When importing on the new Mac, you'll simply authenticate once via your browser).*

---

### Step 2: On Your New Mac (Target)

1. **Import your profile**:

   - **Option A: Turnkey Active Login Transfer (Recommended - Zero Web Logins!)**
     ```bash
     cd ~/ClaudeProfileMover
     ./claude-mover.sh import ~/claude-profile-*.tar.gz --clean --auth-key "<COPIED_KEY>"
     ```
     *In a single step, this registers the Keychain key with open ACL (`-A`), purges stale caches, installs active session cookies (`Cookies`, `sessionKeyV3`, `config.json`), and remaps all paths.*

   - **Option B: Standard Import (Manual Browser Login)**
     ```bash
     cd ~/ClaudeProfileMover
     ./claude-mover.sh import ~/claude-profile-*.tar.gz --clean
     ```
     *(If you prefer not to touch the Keychain, authenticate once in your browser when opening Claude; all imported sessions and memories appear immediately).*

2. **Verify & Launch**:
   ```bash
   ./claude-mover.sh verify
   ```
   Open **Claude Desktop**—your chats, Cowork workspaces, project memories, and MCP servers are ready to use!

---

## Supported Profiles & Setups

- **Claude Desktop (Default)**: Official Anthropic app (`/Applications/Claude.app`, data in `~/Library/Application Support/Claude`).
- **Claude Code CLI**: `~/.claude` (project memories, chat history, and MCP configurations).
- **Multi-Account Profiles (Optional)**: Automatically detects secondary profiles (such as those created with [`two-claude-accounts-mac`](https://github.com/MelkonTech/two-claude-accounts-mac), which launches a secondary instance with `--user-data-dir`, often named `Claude Work` or `ClaudeWork`). The tool auto-detects these secondary profiles, can export them with `--profile claudework`, and can mirror them to standard `Claude.app` on a target Mac.

---

## Advanced: Multi-Account Setups (Running Two Accounts on macOS)

Anthropic distributes only one official desktop app: **Claude Desktop**. If you need to run two accounts simultaneously (e.g. personal and work), here's one you can use: [`two-claude-accounts-mac`](https://github.com/MelkonTech/two-claude-accounts-mac). It duplicates `/Applications/Claude.app` into `/Applications/Claude Work.app` and launches it with `--user-data-dir="~/Library/Application Support/ClaudeWork"`.

**Claude Profile Mover** seamlessly supports multi-account workflows:

### Exporting a Secondary Profile
```bash
./claude-mover.sh export --profile claudework
```

### Running Both Accounts on the New Mac
1. **Create the second app bundle** on the new Mac:
   ```bash
   git clone https://github.com/MelkonTech/two-claude-accounts-mac.git
   cd two-claude-accounts-mac
   ./claude-clone.sh --name "Claude Work"
   ```
2. **Import your profile** using `claude-mover.sh`:
   ```bash
   cd ~/ClaudeProfileMover
   ./claude-mover.sh import ~/claude-profile-claudework-*.tar.gz --clean --auth-key "<COPIED_KEY>"
   ```
Both apps will run side-by-side:
- `/Applications/Claude.app` (Account 1)
- `/Applications/Claude Work.app` (Account 2)

### Migrating a Work Profile to Standard Claude Desktop
If your new Mac only has the standard `Claude.app` installed, `claude-mover.sh import` automatically mirrors the imported profile across both `ClaudeWork` and `Claude` directories, so your chats and login will work immediately inside standard `Claude.app`.

---

## 🔐 Technical Deep Dive: macOS Claude Authentication

Electron apps on macOS use three interconnected layers for authentication:

1. **The Web Layer (`Cookies` & `Local Storage`)**:
   - Claude Desktop is an Electron container running the `claude.ai` web frontend.
   - The web session is stored in `~/Library/Application Support/Claude/Cookies` (storing `sessionKey`, `sessionKeyV3`, `lastActiveOrg`, and `anthropic-device-id`) and `Local Storage/leveldb`.
   - If these web cookies are missing during a transfer, the desktop app shows the web login page.

2. **The Hardware Encryption Layer (`OSCrypt` & macOS Keychain)**:
   - Chromium encrypts values in `Cookies` and `config.json` (`oauth:tokenCacheV2`) with AES-128-CBC using a key derived from the macOS Keychain:
     - **Service**: `Claude Safe Storage`
     - **Account**: `Claude` and `Claude Key`
   - The key is derived using PBKDF2 HMAC-SHA1 (`salt="saltysalt"`, `iterations=1003`).
   - Transferring `config.json` without the Keychain password results in decryption failure on the new Mac.

3. **Keychain Access Control Lists (ACL) & The `-A` Flag**:
   - When generic passwords are added via macOS `security add-generic-password` without `-A`, access is restricted strictly to the terminal binary that created it.
   - `claude-mover.sh set-auth-key` uses the **`-A`** flag, allowing `Claude.app` (and any cloned apps) to read the key without security popups.

---

## Command Reference

| Command | Description |
| :--- | :--- |
| `./claude-mover.sh export` | Package Claude Desktop profile into a portable archive |
| `./claude-mover.sh import <archive>` | Restore a profile archive onto the current Mac with path remapping |
| `./claude-mover.sh get-auth-key` | Read the `Claude Safe Storage` encryption key from the local macOS Keychain |
| `./claude-mover.sh set-auth-key <key>` | Register the Safe Storage key into macOS Keychain with open application permissions (`-A`) |
| `./claude-mover.sh verify` | Run a diagnostic health check on local Claude profiles, sessions, and MCP configs |
| `./claude-mover.sh list` | List all discovered profiles, sessions, Cowork logs, CLI projects, and MCP servers |
| `./claude-mover.sh backup` | Create an instant timestamped safety backup of all local Claude profiles |
| `./claude-mover.sh remap` | Fix hardcoded usernames and paths in-place for existing directories |

### Export Options

- `--profile {claude, claudework, all, auto}`: Profile to package (default: `claude`).
- `-o`, `--out <PATH>`: Custom output archive filename.
- `--no-cli`: Exclude `~/.claude` CLI configurations.
- `--no-key`: Skip retrieving macOS Keychain Safe Storage encryption key (defaults to retrieving it).
- `--dry-run`: Simulate export without creating files.

### Import Options

- `--clean`, `-c`: Removes old placeholder sessions and stale caches from the target before installing.
- `--auth-key <KEY>`: Automatically configures the target Keychain to enable immediate active login without web re-auth.
- `--to-app {claude, claudework, auto}`: Target a specific app directory.
- `--no-mirror`: Disables automatic mirroring between `Claude` and `ClaudeWork` folders.
- `--remap-path "<old>:<new>"`: Add custom path substitutions if folder structures differ.

---

## Interactive Menu

You can run `./claude-mover.sh` (or `python3 claude_profile_mover.py`) with no arguments at any time to get an interactive terminal menu:

```
   ____ _                 _        ____             __ _ _       __  __                      
  / ___| | __ _ _   _  __| | ___  |  _ \ _ __ ___  / _(_) | ___  |  \/  | _____   _____ _ __ 
 | |   | |/ _` | | | |/ _` |/ _ \ | |_) | '__/ _ \| |_| | |/ _ \ | |\/| |/ _ \ \ / / _ \ '__|
 | |___| | (_| | |_| | (_| |  __/ |  __/| | | (_) |  _| | |  __/ | |  | | (_) \ V /  __/ |   
  \____|_|\__,_|\__,_|\__,_|\___| |_|   |_|  \___/|_| |_|_|\___| |_|  |_|\___/ \_/ \___|_|   
        macOS Profile Clone & Migration Tool for Claude Desktop v1.1.0

Detected Claude installations: claude, claude_cli

Please choose an action:
  [1] Export Claude Desktop profile (Standard)
  [2] Export Claude Work / Multi-Account clone
  [3] Export All Claude Profiles & CLI state
  [4] Import a profile archive on this Mac
  [5] Verify / Health check existing profiles
  [6] Remap old username in-place
  [7] List sessions and MCP servers
  [8] Backup current profiles
  [9] Show Keychain Safe Storage Key (for transferring active login)
  [10] Set Keychain Safe Storage Key on this Mac
  [q] Quit
```

---

## Requirements

- **macOS** 12 (Monterey) or later (Apple Silicon & Intel)
- **Python 3** (pre-installed on macOS)
- Zero third-party dependencies (`pip install` is **not** required)

---

## License

MIT License. Free to use, adapt, and distribute.
