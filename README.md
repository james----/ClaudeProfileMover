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
- **Claude in Chrome Browser Add-on**: Extension ID `fcoeoabgfenejglbffodgkkbkcdhcgfn` (LevelDB settings, session cookies, and Native Messaging Host configuration).
- **Claude Code CLI**: `~/.claude` (project memories, chat history, and MCP configurations).
- **Multi-Account Profiles (Optional)**: Automatically detects secondary profiles (such as those created with [`two-claude-accounts-mac`](https://github.com/MelkonTech/two-claude-accounts-mac), which launches a secondary instance with `--user-data-dir`, often named `Claude Work` or `ClaudeWork`). The tool auto-detects these secondary profiles, can export them with `--profile claudework`, and can mirror them to standard `Claude.app` on a target Mac.

---

## 🌐 Claude in Chrome Browser Add-on Support

The tool includes first-class support for migrating and configuring the official **Claude in Chrome** browser extension (`fcoeoabgfenejglbffodgkkbkcdhcgfn`):

### Why Chrome Extension Migration is Tricky
1. **Profile Number Discrepancies**: On the source Mac, your authenticated profile might be `Profile 2` (`user@example.com`), while on the target Mac, the intended profile might be `Profile 1`, `Profile 6`, `Profile 14`, or `Default` (`colleague@example.org`). Claude Profile Mover parses Chrome's internal account registry and **matches by email address**, completely abstracting away internal profile numbers.
2. **Broken Native Messaging Host Paths**: The extension communicates with Claude Desktop via a Native Messaging Host (`com.anthropic.claude_browser_extension.json`). This file hardcodes the absolute binary path to `chrome-native-host`. If the destination Mac uses a different app name (e.g. `Claude Work.app` or `ClaudeWork.app`), the extension cannot talk to desktop Claude. Claude Profile Mover automatically detects the installed Claude app binary on the destination Mac and repairs the manifest.
3. **LevelDB Locks & Cookies**: Ephemeral LevelDB `.LOCK` files are safely stripped to prevent lock contention, and active `claude.ai` and `anthropic.com` session cookies are merged into the target Chrome cookies database without overwriting any of the user's other browser cookies.

### Exporting Chrome Extension Data
- **Included by default** in standard exports (`./claude-mover.sh export`).
- **Target a specific Chrome profile by email or folder**:
  ```bash
  ./claude-mover.sh export --chrome-profile user@example.com
  ```
- **Export Chrome extension data standalone**:
  ```bash
  ./claude-mover.sh export --profile chrome --chrome-profile user@example.com
  ```

### Importing into a Specific Target Profile (by Email)
Even if the destination profile number is completely different on the target computer:
```bash
./claude-mover.sh import claude-profile-*.tar.gz --chrome-to-profile colleague@example.org
```
The tool searches all Chrome profiles on the destination Mac, locates the one signed into `colleague@example.org`, installs the LevelDB extension settings and cookies, and configures the native messaging host.

### Standalone Chrome Native Host Repair
If the extension ever reports "Claude Desktop not found" or you renamed the app bundle:
```bash
./claude-mover.sh setup-chrome
```

### 🔑 Migrating Claude in Chrome Active Authentication (Zero Logins!)

Chromium extensions use **in-memory RAM storage** (`chrome.storage.session`) for active OAuth bearer tokens (`accessToken`, `refreshToken`, `tokenExpiry`). Because Chrome never writes RAM tokens to disk, simply copying extension files leaves the sidepanel prompting you to sign in.

Furthermore, official Chrome Web Store installations run a startup cleanup routine (`SavedPromptsService.js`) that purges session storage upon service worker restart, and Chrome's built-in CRX integrity check prevents editing Web Store files in place.

**Claude Profile Mover** solves this completely by generating a **persistent, self-authenticating unpacked extension** that retains your login across Chrome restarts forever.

---

#### Step 1: On Your Current Mac (Machine A - Source)

Export your active session credentials using either method below:

- **Method A: Fast 10-Second Copy/Paste (Recommended — No Chrome Restart Required!)**
  ```bash
  cd ~/ClaudeProfileMover
  ./claude-mover.sh export-chrome-auth
  ```
  Follow the simple on-screen prompts:
  1. Open `chrome://extensions` in Chrome.
  2. Toggle **Developer mode** to **ON** (top-right corner).
  3. Under **Claude**, click `service worker` to open DevTools.
  4. Paste the printed one-line JavaScript command into the Console tab and press **Enter**.
  5. Paste the output line into your terminal. Saved to `claude-chrome-auth.json`!

- **Method B: Hands-Free Automated Export (`--auto`)**
  ```bash
  cd ~/ClaudeProfileMover
  ./claude-mover.sh export-chrome-auth --auto
  ```
  *The script automatically connects to Chrome via Chrome DevTools Protocol. If Chrome is not already running with remote debugging enabled, the script will automatically offer to launch/restart Chrome with debugging on port 9222, extract the active tokens hands-free, and save `claude-chrome-auth.json`!*

---

#### Step 2: Transfer the File

Copy `claude-chrome-auth.json` from Machine A to Machine B (via AirDrop, USB drive, or secure copy).

---

#### Step 3: On Your New Mac (Machine B - Destination)

1. Make sure you have installed the Claude in Chrome extension from the Chrome Web Store at least once (so its base assets are present).
2. Run the persistent builder:
   ```bash
   cd ~/ClaudeProfileMover
   ./claude-mover.sh persist-chrome-auth claude-chrome-auth.json
   ```
   *This automatically creates `~/Desktop/Claude-Extension`, strips Web Store CRX signature locks, neutralizes the self-wiping cleanup code, embeds your authenticated session payload, and reveals the folder in Finder.*

---

#### Step 4: Activate in Chrome (Foolproof Checklist)

*The script automatically opens Chrome to `chrome://extensions`, highlights the folder in Finder, and copies `~/Desktop/Claude-Extension` directly to your clipboard!*

1. In Chrome (already opened to `chrome://extensions`), ensure **Developer mode** is toggled **ON** in the top-right corner.
2. In the top-left corner, click **Load unpacked**.
3. In the file picker dialog:
   - Press `Cmd+Shift+G`, press `Cmd+V` (the path is already on your clipboard!), and press **Enter**.
   - *(Or click **Desktop** in the Finder sidebar and select **Claude-Extension**).*
4. 🔍 **Notice: You will now see TWO cards on the extensions page:**
   - **`Claude (Persistent)`**: This is the new migrated extension created by the script. **Keep this toggle ON (enabled)!**
   - **`Claude`**: This is the original Chrome Web Store version. **Toggle this card OFF (disabled)** so Chrome doesn't run two conflicting copies of Claude simultaneously.
5. 🧩 **Crucial Gotcha — Where is the extension icon on your toolbar?**
   - Chrome does **NOT** place newly loaded unpacked extensions on the toolbar by default.
   - Click the **Extensions menu (🧩 puzzle piece icon)** in the top-right corner of Chrome's toolbar.
   - Locate **`Claude (Persistent)`** in the dropdown and click the **Pin icon (📌)**.
   - The Claude icon will now stay permanently pinned to your toolbar!
6. Click the Claude toolbar icon: **You are immediately logged in with full access!**

> [!IMPORTANT]
> **Keep the `Claude-Extension` folder on your Desktop!**
> Chrome runs unpacked extensions directly from the folder on disk. Do **not** delete, rename, or move the `~/Desktop/Claude-Extension` folder after loading it.

---

### 💡 Demystifying "Classic Mode" vs. "The New Thing" (Cowork Experience)

One of the biggest points of confusion with Claude in Chrome is Anthropic's recent introduction of two completely different operating modes inside the exact same extension sidepanel:

#### 1. "The New Thing" (Cowork Experience)
Anthropic recently added an experimental preview called the **Cowork experience**. Instead of running natively inside the browser, it loads `claude.ai/cic/new` inside an embedded web iframe.
- **The Gotcha**: Because it runs inside a web frame, it looks for an active web browser cookie session on `claude.ai`. If you aren't signed into `claude.ai` in a standard browser tab, the sidepanel will show a confusing prompt:
  > *"The side panel uses your claude.ai session. Sign in on claude.ai to continue."*
- **The Misconception**: Users often see this message and assume the migration failed or that they are logged out. **You are not logged out!** Anthropic simply switched your sidepanel into web preview mode.

#### 2. "Classic Mode" (The Real Extension Experience)
**Classic Mode** is the native, dedicated sidepanel chat. It communicates directly with Anthropic's backend using the transferred OAuth bearer token.
- **Features**: Direct tab reading, page summarization, file attachments, computer-use automation, and full access to Sonnet and Opus under your paid subscription.
- **Zero Web Login Required**: It does not depend on web cookies or having an open `claude.ai` tab.

#### 🛠️ How to Switch to Classic Mode in 2 Clicks
If the sidepanel ever prompts you to sign in to Cowork:
1. Click the **three vertical dots (`⋮`)** at the very top right of the Claude sidepanel (right next to the `X` close button).
2. Click **"Switch back to classic"**.
3. Your full chat history and active subscription will appear immediately!

*(Note: Claude Profile Mover's `persist-chrome-auth` builder automatically pre-configures `preferCoworkExperience: false` in local storage, so your persistent extension defaults to Classic mode right out of the box).*

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
| `./claude-mover.sh export` | Package Claude Desktop & Chrome profiles into a portable archive |
| `./claude-mover.sh import <archive>` | Restore a profile archive onto the current Mac with path & Chrome remapping |
| `./claude-mover.sh setup-chrome` | Configure or repair the Chrome Native Messaging Host binary path registration |
| `./claude-mover.sh export-chrome-auth` | Extract or guide extraction of active Claude in Chrome session tokens |
| `./claude-mover.sh import-chrome-auth` | Restore and inject active session tokens into Claude in Chrome on destination Mac |
| `./claude-mover.sh persist-chrome-auth` | Create a persistent unpacked Claude in Chrome extension that survives restarts forever |
| `./claude-mover.sh get-auth-key` | Read the `Claude Safe Storage` encryption key from the local macOS Keychain |
| `./claude-mover.sh set-auth-key <key>` | Register the Safe Storage key into macOS Keychain with open application permissions (`-A`) |
| `./claude-mover.sh verify` | Run a diagnostic health check on local Claude profiles, Chrome extensions, and MCP configs |
| `./claude-mover.sh list` | List all discovered profiles, sessions, Chrome profiles, CLI projects, and MCP servers |
| `./claude-mover.sh backup` | Create an instant timestamped safety backup of all local Claude profiles |
| `./claude-mover.sh remap` | Fix hardcoded usernames and paths in-place for existing directories |

### Export Options

- `--profile {claude, claudework, chrome, all, auto}`: Profile to package (default: `claude`).
- `--chrome-profile <EMAIL_OR_DIR>`: Specific Chrome profile to export (e.g. `user@example.com` or `Profile 2`).
- `--no-chrome`: Skip packaging Claude in Chrome browser extension data.
- `-o`, `--out <PATH>`: Custom output archive filename.
- `--no-cli`: Exclude `~/.claude` CLI configurations.
- `--no-key`: Skip retrieving macOS Keychain Safe Storage encryption key (defaults to retrieving it).
- `--dry-run`: Simulate export without creating files.

### Import Options

- `--chrome-to-profile <EMAIL_OR_DIR>`: Target Chrome profile to restore extension data into (e.g. `colleague@example.org` or `Profile 1`). Auto-matches even when profile numbers differ!
- `--chrome-from-profile <EMAIL_OR_DIR>`: Choose specific Chrome profile from archive to restore from.
- `--no-chrome`: Skip restoring Claude in Chrome browser extension data.
- `--clean`, `-c`: Removes old placeholder sessions and stale caches from the target before installing.
- `--auth-key <KEY>`: Automatically configures the target Keychain to enable immediate active login without web re-auth.
- `--to-app {claude, claudework, auto}`: Target a specific app directory.
- `--no-mirror`: Disables automatic mirroring between `Claude` and `ClaudeWork` folders.
- `--remap-path "<old>:<new>"`: Add custom path substitutions if folder structures differ.

### Chrome Extension Authentication Options

- **`export-chrome-auth`**:
  - `-o`, `--out <PATH>`: Destination JSON file path (default: `claude-chrome-auth.json`).
  - `--auto`: Automate token extraction hands-free using Chrome DevTools Protocol (CDP WebSocket).
  - `--port <PORT>`: Remote debugging port for CDP (default: `9222`).
- **`persist-chrome-auth [auth_file]`**:
  - `auth_file`: Path to auth file (defaults to `claude-chrome-auth.json` or `~/claude-chrome-auth.json`).
  - `--out-dir <DIR>`: Custom destination for the permanent unpacked extension (default: `~/Desktop/Claude-Extension`).
  - `--chrome-profile <EMAIL_OR_DIR>`: Specific Chrome profile directory to copy base extension assets from.

---

## Interactive Menu

You can run `./claude-mover.sh` (or `python3 claude_profile_mover.py`) with no arguments at any time to get an interactive terminal menu:

```
   ____ _                 _        ____             __ _ _       __  __                      
  / ___| | __ _ _   _  __| | ___  |  _ \ _ __ ___  / _(_) | ___  |  \/  | _____   _____ _ __ 
 | |   | |/ _` | | | |/ _` |/ _ \ | |_) | '__/ _ \| |_| | |/ _ \ | |\/| |/ _ \ \ / / _ \ '__|
 | |___| | (_| | |_| | (_| |  __/ |  __/| | | (_) |  _| | |  __/ | |  | | (_) \ V /  __/ |   
  \____|_|\__,_|\__,_|\__,_|\___| |_|   |_|  \___/|_| |_|_|\___| |_|  |_|\___/ \_/ \___|_|   
        macOS Profile Clone & Migration Tool for Claude Desktop v1.2.0

Detected Claude installations: claude, claudework, claude_cli
Detected Claude in Chrome: Chrome [Profile 2] (user@example.com)

Please choose an action:
  [1] Export Claude Desktop profile (Standard)
  [2] Export Claude Work / Multi-Account clone
  [3] Export All Claude Profiles & CLI state
  [4] Import a profile archive on this Mac
  [5] Verify / Health check existing profiles & Chrome Extension
  [6] Remap old username in-place
  [7] List sessions, MCP servers & Chrome profiles
  [8] Backup current profiles
  [9] Show Keychain Safe Storage Key (for transferring active login)
  [10] Set Keychain Safe Storage Key on this Mac
  [11] Setup / Repair Claude in Chrome Native Messaging Host
  [12] Export Claude in Chrome extension data only
  [13] Export Claude in Chrome active session tokens
  [14] Import / Restore Claude in Chrome session tokens
  [15] Make Claude in Chrome authentication permanent (Load unpacked)
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
