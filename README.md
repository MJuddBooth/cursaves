# Cursaves+

[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-support-yellow?style=flat&logo=buy-me-a-coffee)](https://buymeacoffee.com/callumward)

**Cursaves+** is an enhanced fork of [cursaves](https://github.com/Callum-Ward/cursaves) by **[Callum Ward](https://github.com/Callum-Ward)** — the original author and copyright holder of the base project.

## North star: two devices, zero manual sync

Use **Cursor on two PCs at the same time** with no babysitting:

1. **Global profile** stays identical — settings (dark mode), keybindings, skills, commands, agents, hooks, rules, CLI config
2. **Project chats** sync via git remote identifier (same `origin` = same snapshot bucket)
3. **Auto-sync** runs while Cursor is open (~120s interval via `watch --install-hook`)

**Typical flow:**

| Step | PC1 | PC2 |
|------|-----|-----|
| Setup (once) | `cursaves auth github` + `cursaves watch --install-hook` | Same on PC2 |
| Work on project A | Open repo, chat → auto-push snapshots | Open same repo → auto-pull + import chats |
| Work on project B | — | Open repo B, chat → auto-push |
| See PC2's chats | `git clone` project B, open in Cursor → chats import on next sync | — |

One private `cursaves-data` repo is the hub for **profile + all project chats**. Project identity comes from the git remote URL, not your local folder path.

**Checklist (both machines, one time):**

```bash
cursaves auth github          # login + sync remote
cursaves watch --install-hook # auto-sync when Cursor opens
```

Restart Cursor after imports or profile changes to see them in the sidebar.

**Repo hygiene (optional):** `cursaves remove` stops syncing a chat forever; `cursaves pin` protects favorites from retention; `cursaves config sync --retention-days 90` prunes old snapshots (default 90, `0` = off). Project rules in `.cursor/rules` inside a repo sync with that repo's git — not via cursaves-data.

---

Cursor stores chats locally. Switch machines and they're gone. This tool saves your chats to a git repo (or S3 bucket) so you can restore them anywhere — or copy them between workspaces on the same machine.

## Cursaves+ features

This fork extends the original cursaves with:

| Feature | Description |
|---------|-------------|
| **Login with GitHub** | One browser login sets up push/pull auth, commit identity, and HTTPS sync remote via [GitHub CLI](https://cli.github.com/) (`gh`). GUI/setup can auto-install `winget` + `gh` on Windows |
| **Desktop GUI** | Running `cursaves` with no args opens a CustomTkinter app (Dashboard, Sync, Auto-sync, Profile, Info, Tools, Setup) |
| **Profile sync** | Sync settings, keybindings, snippets, skills, commands, agents, hooks, rules, and CLI config via the same private remote (`profile push/pull/status`, included in `sync`) |
| **Git commit identity** | Per-repo identity for `~/.cursaves/` commits (name/email, GPG off by default) — auto-filled from GitHub login or `cursaves config git` |
| **Windows support** | Full support on Windows (`%APPDATA%` paths, one-click `Install Cursaves.bat`) |
| **Setup wizard** | `cursaves setup` — interactive first-run (GitHub login, remote, profile, hook) |
| **One-click install** | `Install Cursaves.bat` / `Install Cursaves.command` + bootstrap scripts (`scripts/setup.ps1`, `scripts/setup.sh`) |
| **Auto-sync on Cursor open** | `cursaves watch --install-hook` — `sessionStart` hook + background `watch --all` daemon |
| **Desktop shortcuts** | Create shortcuts from the GUI Setup tab |
| **Interactive TUI** | Fuzzy multi-select for push/pull/copy/purge (`-s` flags, InquirerPy) |

New commands: `auth github`, `config git`, `config sync`, `setup`, `remove`, `pin`, `profile push|pull|status`, `gui`, `watch --install-hook|--uninstall-hook|--all|--detach`.

Based on [cursaves](https://github.com/Callum-Ward/cursaves) by **Callum Ward** — all upstream features (sync, push/pull, SSH workspaces, doctor, migrate, purge, etc.) are included unchanged.

## How It Works

### Terminology

| Term               | Meaning                                                                                                                   |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------- |
| **Chat**           | A conversation with the AI in Cursor                                                                                      |
| **Workspace**      | Cursor creates one for each directory (or `.code-workspace` file) you open. Chats belong to workspaces.                   |
| **Workspace hash** | The opaque directory name under `workspaceStorage/`; use with `-w` when number/path doesn't work (e.g. custom workspaces) |
| **Project ID**     | How cursaves groups snapshots - based on git remote URL or directory name                                                 |
| **Snapshot**       | An exported chat saved to `~/.cursaves/snapshots/<project-id>/`                                                           |

### Chat → Workspace → Project Mapping

**Cursor stores chats per workspace (per directory path):**

```
/Users/alice/repos/myapp     → Workspace A → [chat1, chat2, chat3]
/Users/bob/repos/myapp       → Workspace B → [chat4, chat5]
ssh://core/home/user/myapp   → Workspace C → [chat6, chat7]
```

Each workspace is a unique path. Even the same repo cloned to different locations creates separate workspaces with separate chats.

**cursaves groups snapshots by project identifier (git remote URL):**

```
All three workspaces above have the same git remote:
  git@github.com:user/myapp.git

So all their chats get saved to:
  ~/.cursaves/snapshots/github.com-user-myapp/
```

**On import, cursaves matches snapshots to local workspaces by path:**

```
Machine A exports chat from: /Users/alice/repos/myapp
Machine B imports chat into: /Users/bob/repos/myapp  (same project ID, different path)
  → Paths in chat metadata are rewritten automatically
```

This means you can sync chats for the same repo across different machines, even if the local paths differ.

## Quick Start

### Recommended: Login with GitHub

One login configures **everything** for the sync repo — same GitHub account for push/pull, commit author, and remote URL (HTTPS, no SSH keys or GPG prompts):

```bash
# Install globally (once per machine)
uv tool install git+https://github.com/JoaoFernandes02/cursavesplus.git

# Install GitHub CLI (optional on Windows/macOS — GUI and setup can install automatically)
# Windows: winget + gh installed for you from Setup → Login with GitHub
# macOS:   brew install gh  (or auto-install from GUI/setup if Homebrew is present)
# Linux:   see https://github.com/cli/cli#installation

# Login (browser opens) — then choose existing repo or auto-create cursaves-data
cursaves auth github

# Sync chats + profile
cursaves sync
```

**GUI:** run `cursaves` → **Setup** tab → **Login with GitHub**.

Flow in the GUI:

1. A popup shows the **8-character GitHub code**; the browser opens to `github.com/login/device`
2. Paste the code on GitHub and authorize — the popup closes automatically when login completes
3. Setup shows `@your-github-username`; you choose an existing sync repo or let cursaves create `cursaves-data`
4. **Sync / Push / Pull** are enabled only after GitHub login **and** sync repo setup are complete

Until setup is complete, sync actions show a message in the Output panel and redirect you to **Setup**.

On **Windows**, if `winget` or `gh` are missing, the GUI asks once and installs both automatically. The CLI (`cursaves auth github`) and `cursaves setup` offer the same install prompt.

**What `auth github` does:**

| Step | Result |
|------|--------|
| `gh auth login` | GitHub session for your account |
| `gh auth setup-git` | Git Credential Manager handles HTTPS push/pull |
| Profile API | Sets commit author (`user.name` / `user.email`) in `~/.cursaves/` |
| Remote setup | HTTPS URL on your account (existing or new private `cursaves-data` repo) |

Config is saved to `~/.config/cursaves/config.json` under `github` and `git` (always the same account).

```bash
cursaves auth github --status     # show @login and remote
cursaves auth github --logout     # sign out
cursaves auth github --remote URL # use an existing private repo
cursaves auth github --create-repo
```

### Alternative: full setup wizard

```bash
cursaves setup          # interactive (includes GitHub login)
cursaves setup --yes    # non-interactive defaults (requires gh already logged in)
```

### Legacy: manual git / SSH

If you prefer SSH or a custom setup without `gh`:

```bash
cursaves init --remote git@github.com:you/cursaves-data.git
cursaves config git --name "You" --email "you@example.com" --no-sign
```

Or HTTPS without `gh` (use Git Credential Manager when prompted):

```bash
cursaves init --remote https://github.com/you/cursaves-data.git
cursaves config git --name "You" --email "you@example.com" --no-sign
```

Then from any project directory:

```bash
# Automatic bidirectional sync (pull behind + push ahead)
cursaves sync

# Verbose git output if sync fails
cursaves sync --verbose
# or: set CURSAVES_DEBUG=1

# Or manually:
cursaves push              # save and push to remote
cursaves pull              # pull and restore conversations
# Then restart Cursor (quit and reopen) to see the imported chats
```

For SSH remote projects (or custom workspaces), Cursor stores chats on your local machine. Use `-w` to target a workspace:

```bash
# See all workspaces (local, SSH remote, custom .code-workspace)
cursaves workspaces

# Push/pull a specific workspace by number, hash, or path substring
cursaves push -w 3
cursaves push -w 497e8ab0   # by hash (from the Hash column)
```

`push` checkpoints your conversations and pushes to the remote. `pull` fetches from the remote and imports into Cursor's database. `sync` does both automatically — pulling conversations where your local copy is behind, and pushing ones where your local copy is ahead. After importing, restart Cursor (quit and reopen) to see the conversations.

### Example

```
$ cursaves push

Checkpointing conversations for /Users/you/Projects/my-app...
  3 conversation(s) checkpointed
  Committed
  Pushing... done

Done. 3 conversation(s) saved and pushed.
```

```
$ cursaves list

Conversations for /Users/you/Projects/my-app

ID                                       Name                           Mode      Msgs  Last Updated
--------------------------------------------------------------------------------------------------------------
fda95e1a-7d3a-4113-942f-7e033e454bef     Project structure and iss...   agent     1203  2026-01-19 20:11 UTC
cadfb263-3326-4aff-8887-dcc12f736b11     Feedback on documentation...   agent      595  2025-12-15 12:36 UTC
76b5729a-375a-4e07-ba38-d58b322c85fc     Adjust layout for better ...   agent      317  2025-10-02 11:19 UTC

3 conversation(s) total
```

## Installation

**Requirements:** Python 3.10+, [uv](https://docs.astral.sh/uv/), macOS/Linux/Windows, Git, [GitHub CLI](https://cli.github.com/) (`gh`) for Login with GitHub.

**Tested with:** Cursor 2.6–3.0 (supports both old and new chat storage formats)

### Install as a global CLI tool (recommended)

```bash
# Standard install (git backend only)
uv tool install git+https://github.com/JoaoFernandes02/cursavesplus.git

# With S3 support
uv tool install "cursaves[s3] @ git+https://github.com/JoaoFernandes02/cursavesplus.git"
```

This puts `cursaves` on your PATH so you can run it from any directory. Run this on each machine you want to sync between.

If `~/.local/bin` is not on your PATH, run `uv tool update-shell` or add it manually.

### Update

```bash
uv tool upgrade cursaves
```

### Alternative: clone and run locally

```bash
git clone git@github.com:JoaoFernandes02/cursavesplus.git
cd cursavesplus
uv sync
uv run cursaves <command>

# Or install from local clone:
uv tool install .

# Or without uv:
python -m cursor_saves <command>
```

## Setup

`cursaves` stores conversation snapshots locally at `~/.cursaves/snapshots/`. To sync between machines, configure a **backend** — git (default) or S3.

### Option A: Git backend + Login with GitHub (recommended)

1. On each machine, open the GUI (**Setup → Login with GitHub**) or run `cursaves auth github`. On Windows, cursaves can install `winget` and `gh` for you if they are missing.
2. You'll be asked whether you already have a private sync repo. If not, cursaves creates `https://github.com/<you>/cursaves-data` automatically.
3. Run `cursaves sync` from any project directory.

**One account, three roles:** the same GitHub login handles remote authentication (push/pull), commit author identity, and repo access. GPG signing is disabled for the sync repo by default (avoids passphrase prompts).

To inspect or change commit identity without re-login:

```bash
cursaves config git --show
cursaves config git --name "You" --email "you@example.com" --no-sign
```

### Option B: Git backend (manual / SSH)

1. Create a **private** repository on GitHub/GitLab (empty, no README).
2. Initialize on each machine:

```bash
cursaves init --remote git@github.com:you/cursaves-data.git
cursaves config git --name "You" --email "you@example.com" --no-sign
```

This creates `~/.cursaves/` with a git repo and the remote configured. Commit identity is stored locally in the sync repo only (does not change your global git config).

If you only want local checkpoints (no syncing), run `cursaves init` without `--remote`.

### Option C: S3 backend

1. Create an S3 bucket (private).
2. Configure AWS credentials (`aws configure`, env vars, or IAM role).
3. Install with S3 support and initialize:

```bash
uv tool install "cursaves[s3] @ git+https://github.com/JoaoFernandes02/cursavesplus.git"
cursaves init --backend s3 --bucket my-cursor-saves --region us-east-1
```

S3 avoids git history overhead and works well for large snapshot sets. Authentication uses the standard AWS credential chain.

### Start syncing

```bash
# Automatic bidirectional sync (recommended)
cursaves sync

# Or manually:
cursaves push              # checkpoint + push
cursaves pull              # pull + import into Cursor's database
# Then restart Cursor to see the imported conversations
```

The `sync` command pulls conversations where your local copy is behind the remote, then pushes conversations where your local copy is ahead — fully automatic, no prompts.

## Commands

All commands default to the current working directory as the project path. Use `-w <selector>` to target a workspace by number, hash, or path substring (from `cursaves workspaces`), or `-p /path` to specify a path directly.

| Command      | Description                                                 | Modifies Cursor data? |
| ------------ | ----------------------------------------------------------- | --------------------- |
| **`sync`**     | **Pull behind + push ahead — one command to stay in sync**  | Yes                   |
| **`sync -v`**  | **Same as sync, with verbose git command logging**          | Yes                   |
| **`auth github`** | **Login with GitHub — auth + identity + remote**         | No                    |
| **`config git`** | **Set/show commit identity for ~/.cursaves/**             | No                    |
| **`push`**     | **Checkpoint + push to remote**                             | No                    |
| **`push -s`**  | **Interactively select which conversations to push**        | No                    |
| **`push --id`** | **Push just the chat(s) with this ID (or unique prefix)**   | No                    |
| **`pull`**     | **Pull from remote + import snapshots**                     | Yes                   |
| `setup`        | Interactive first-run wizard (GitHub login, profile, hook) | No                    |
| `init`         | Initialize sync repo (git remote or S3 bucket)              | No                    |
| `workspaces`   | List all Cursor workspaces (local, SSH, custom) with hash   | No                    |
| `list`         | Show conversations for a project                            | No                    |
| `snapshots`    | List snapshot projects available in ~/.cursaves/           | No                    |
| `status`       | Compare local conversations vs snapshots                    | No                    |
| `repair`       | Restore missing agent blobs from snapshots                  | Yes                   |
| `delete`       | Delete cached snapshots (interactive, by ID, or all)        | No                    |
| `export <id>`  | Export one conversation to a snapshot                       | No                    |
| `checkpoint`   | Export all conversations (no push)                          | No                    |
| `import --all` | Import snapshots (no pull)                                  | Yes                   |
| `watch`        | Auto-checkpoint and sync in the background                  | No (reads only)       |
| `copy`         | Copy conversations between workspaces (same machine)        | Yes                   |
| `doctor`       | Audit chats: find orphaned/lost conversations, recover them | Yes (with `--recover`) |
| `migrate`      | Migrate old chats to Cursor 3.0 global index                | Yes                   |
| `purge`        | Delete chats from Cursor's DB to reclaim disk space         | Yes                   |

Most of the time you only need `sync`. On a new machine, run `cursaves auth github` once, then `sync`. Use `push -s` when you want to pick conversations from a list, or `push --id a39071a0` (repeatable, any unique prefix of the chat ID) when you already know which one you want; it finds the chat's workspace itself, so it works from any directory. Use `repair` if you get "Blob not found" errors after importing. Use `doctor` to find and recover orphaned chats. Use `migrate` after updating to Cursor 3.0 to make all old chats visible in the sidebar. Use `purge` to delete chats and reclaim disk space (requires Cursor to be closed). Use `delete` to clean up snapshots you no longer need.

### Git sync troubleshooting

If `cursaves sync` fails with a git error:

```bash
cursaves sync --verbose          # show git commands and stderr
# or
set CURSAVES_DEBUG=1             # Windows
export CURSAVES_DEBUG=1          # macOS/Linux
cursaves sync
```

| Error | Fix |
|-------|-----|
| `Host key verification failed` | Use `cursaves auth github` (HTTPS) instead of SSH, or run `ssh-keyscan github.com >> ~/.ssh/known_hosts` |
| `Could not read from remote` | Run `cursaves auth github` or check repo access with `gh repo view you/cursaves-data` |
| GPG / signing prompts | Run `cursaves config git --no-sign` or re-run `cursaves auth github` (GPG disabled for sync repo) |
| Not logged in | `cursaves auth github --status` then `cursaves auth github` |
| `gh` / `winget` not found | Use **Setup → Login with GitHub** in the GUI (auto-installs on Windows), or `cursaves auth github` / `cursaves setup` |
| Account not shown after GitHub login | Check the **Output** panel for errors; re-run **Login with GitHub**; try `cursaves auth github --status` |
| Sync button does nothing in GUI | Complete full setup: GitHub login + sync repo (Setup tab). Output panel explains what is missing |

### Auto-sync with `watch`

```bash
# Run in a terminal on each machine -- handles everything automatically
cursaves watch -p /path/to/your/project

# Options
cursaves watch --interval 30     # check every 30s (default: 60)
cursaves watch --no-git          # checkpoint only, no git push/pull
cursaves watch --verbose         # log every check, not just changes
```

The watch daemon polls for database changes, auto-checkpoints when conversations update, and commits + pushes to git. On the other end, it pulls and picks up new snapshots.

## How Cursor Stores Chat Data

Cursor stores conversations in two local SQLite databases, not as files you can easily copy:

- **Workspace DB** (`workspaceStorage/{id}/state.vscdb`): Links conversations to a workspace. In Cursor ≤2.6, this contains a chat list (`allComposers`). In Cursor 3.0+, this list is removed and replaced by a central index in the global DB.
- **Global DB** (`globalStorage/state.vscdb`): The actual conversation content -- one JSON blob per conversation, keyed by `composerData:{UUID}`. In Cursor 3.0+, also contains `composer.composerHeaders` -- a central index mapping every chat to its workspace.

> **Cursor 3.0 migration (April 2026):** Cursor 3.0 centralized the chat-workspace index from per-workspace DBs into the global DB. cursaves handles both formats transparently. See [docs/how-cursor-stores-chats.md](docs/how-cursor-stores-chats.md) for details.

Data locations:

- macOS: `~/Library/Application Support/Cursor/User/`
- Linux: `~/.config/Cursor/User/`

Notably, **chat data is always stored on the machine running Cursor's UI**, even when connected to a remote host via SSH. This is why switching machines means losing your conversation context.

For more details, see [docs/how-cursor-stores-chats.md](docs/how-cursor-stores-chats.md).

## Cross-Platform Support

### Project identity

Projects are identified by their **git remote origin URL**, not the local directory name. This means:

- `~/Projects/bob` and `~/repos/alice` with the same `origin` are treated as the same project -- conversations sync between them.
- Two unrelated repos both named `myapp` won't collide, because their remotes differ.
- Non-git directories fall back to matching by directory name.

You can see what identity is being used with `cursaves status`.

### Path rewriting

When importing conversations on a different machine, absolute file paths in conversation metadata (e.g., which files were attached as context) are automatically rewritten to match the target project path. The actual conversation content -- your messages and AI responses -- is fully portable with no modification.

For example, a conversation started on macOS at `/Users/you/Projects/myapp` will have its file references rewritten to `/home/you/repos/myapp` when imported on a Linux machine.

## Restarting Cursor After Import

Cursor caches all conversation data in memory at startup and never watches its SQLite files for external changes. After `pull` or `import` writes new conversations to the database, **you must fully restart Cursor** (quit and reopen) to see the imported conversations.

Note: "Developer: Reload Window" is not sufficient -- it reloads the renderer but doesn't re-read the conversation database. A full application restart is required.

## Safety

- **Read operations** (`list`, `export`, `checkpoint`, `status`, `watch`) work on a temporary copy of the database. They never touch Cursor's files and are safe to run while Cursor is open.
- **Write operations** (`import`, `pull`) back up the target database before writing, and refuse to run while Cursor is detected as running. Use `--force` to override (not recommended).
- Snapshots are self-contained JSON -- even if import goes wrong, you always have the raw data and the backup.

## Privacy Warning

Snapshot files contain your **full conversation data**: your prompts, AI responses, file paths from your machine, your machine's hostname, and timestamps.

**Use a private repository** for the `~/.cursaves/` remote. Do not push conversation snapshots to a public repo.

## Typical Workflows

### Local projects

```bash
# On Machine A -- before switching:
cursaves sync       # pushes your ahead conversations

# On Machine B -- first time on this machine:
cursaves auth github   # one-time login (same GitHub account)
cursaves sync          # pulls the latest, pushes anything ahead locally
# Then restart Cursor (quit and reopen) to see the imported conversations
```

### Copying chats between workspaces (same machine)

Cursor isolates chats per workspace. If you clone the same repo to a new directory, or open it from a different path, your previous chats won't be there. `cursaves` can copy them across:

```bash
# Export chats from the old workspace
cd /path/to/old/checkout
cursaves push

# Import into the new workspace
cd /path/to/new/checkout
cursaves pull
# Restart Cursor to see the imported chats
```

This also works with `-s` to selectively pick which conversations to copy, and with `-w` to target specific workspaces without `cd`-ing into them.

No remote repo is needed for this — `cursaves init` (without `--remote`) is enough for local-only use.

### SSH remote projects

When you connect to a remote server via Cursor's SSH feature, **chats are stored on your local machine**, not on the remote server. This means:

- `cursaves` must run **locally** (not on the remote server)
- SSH workspace paths like `/home/user/repos/myapp` don't exist on your local filesystem
- You can't just `cd` into them and run `cursaves push`

**Pushing from SSH workspaces:**

```bash
# Interactive selection (recommended)
cursaves push -s
#  → Shows all workspaces (local + SSH), lets you pick which chats to push

# Or by workspace number, hash, or path
cursaves workspaces          # List workspaces; note the #, Hash, or path
cursaves push -w 3           # By number
cursaves push -w 497e8ab0    # By hash (for custom workspaces)
```

**Pulling into SSH workspaces:**

```bash
# Interactive selection (recommended)
cursaves pull -s
#  → Shows available snapshots by project
#  → Auto-detects matching SSH workspaces
#  → Imports into the correct workspace

# Or by workspace number, hash, or path
cursaves workspaces          # List workspaces; note the #, Hash, or path
cursaves pull -w 3           # By number
cursaves pull -w 497e8ab0    # By hash
```

**Important:** Run these commands in a **local terminal**, not in Cursor's integrated terminal (which runs on the remote server).

**After importing:** Restart Cursor (quit and reopen) to see the chats in your SSH session.

### Custom workspaces (`.code-workspace`)

If you use a VS Code/Cursor custom workspace (e.g. `my-proj.code-workspace`), it may not appear in `cursaves workspaces` with a recognizable path. In that case:

1. Find the workspace hash: browse `~/Library/Application Support/Cursor/User/workspaceStorage/` (macOS) or `~/.config/Cursor/User/workspaceStorage/` (Linux) and locate the directory containing your chats.
2. Use the hash as the workspace selector: `cursaves push -w <hash>` or `cursaves pull -w <hash>`.

`cursaves workspaces` now shows custom workspaces as `(workspace)` and includes a Hash column you can use.

### Automatic sync

```bash
# Run on each machine -- handles everything in the background:
cursaves watch -p /path/to/your/project
```

The daemon handles checkpoint + git push/pull automatically. When you switch machines, conversations are already synced.

## Architecture

```
~/.cursaves/                   # Local snapshot store
  snapshots/
    github.com-user-repo/      # Identified by git remote URL
      <composer-id>.json.gz    # Self-contained conversation snapshot
  .git/                        # Present when using git backend

~/.config/cursaves/
  config.json                  # Backend + GitHub login + git commit identity
  sync_state.json              # Tracks handled diverged snapshots

# Example config.json after Login with GitHub:
# {
#   "backend": "git",
#   "github": {
#     "login": "you",
#     "name": "Your Name",
#     "email": "you@example.com",
#     "remote_url": "https://github.com/you/cursaves-data.git",
#     "repo": "cursaves-data"
#   },
#   "git": {
#     "name": "Your Name",
#     "email": "you@example.com",
#     "sign_commits": false
#   },
#   "profile": { ... }
# }

~/.local/bin/cursaves          # Global CLI tool (installed via uv)

cursaves/                      # Source repo (this repo, public)
  cursor_saves/                # Python package
  docs/
  pyproject.toml
  LICENSE
```

The tool code (this repo) is separate from your conversation data (`~/.cursaves/`). Install the tool once, run `cursaves auth github` (or point at a private remote manually), and sync from any project directory.

## Contributing

**Version bumps are required on every commit.** Users install via `uv tool install git+https://github.com/JoaoFernandes02/cursavesplus.git` and update with `uv tool upgrade cursaves`. The upgrade command compares version numbers -- if the version doesn't change, it won't pull new code even with new commits.

Bump the version in **both** files:

- `pyproject.toml` (`version = "X.Y.Z"`)
- `cursor_saves/__init__.py` (`__version__ = "X.Y.Z"`)

Use [semver](https://semver.org/): patch for fixes, minor for features, major for breaking changes.

## Support

If you find this useful, consider buying me a coffee:

[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-support-yellow?style=for-the-badge&logo=buy-me-a-coffee)](https://buymeacoffee.com/callumward)

## Credits

**Cursaves+** is a fork of [cursaves](https://github.com/Callum-Ward/cursaves).

- **Original author:** [Callum Ward](https://github.com/Callum-Ward) — created cursaves and holds copyright on the original work
- **Fork:** [JoaoFernandes02/cursavesplus](https://github.com/JoaoFernandes02/cursavesplus) — GUI, profile sync, Login with GitHub, Windows support, and other enhancements listed above

This project is a derivative work of cursaves. Source code from the original project is used under the terms of the [AGPL-3.0](LICENSE) license. See the [original repository](https://github.com/Callum-Ward/cursaves) for the upstream project.

If you find the original tool useful, consider [buying Callum a coffee](https://buymeacoffee.com/callumward).

## License

AGPL-3.0. See [LICENSE](LICENSE) for details.

Cursaves+ is based on cursaves © Callum Ward. Modifications in this fork are also licensed under AGPL-3.0.
