"""Platform detection and Cursor storage path resolution."""

import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote, urlparse


def get_cursor_user_dir() -> Path:
    """Return the Cursor User data directory for the current platform.

    macOS:   ~/Library/Application Support/Cursor/User
    Linux:   ~/.config/Cursor/User
    Windows: %APPDATA%/Cursor/User
    """
    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "Cursor" / "User"
    elif system == "Linux":
        base = Path.home() / ".config" / "Cursor" / "User"
    elif system == "Windows":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            print(
                "Error: APPDATA environment variable is not set.\n"
                "Cannot locate Cursor data directory on Windows.",
                file=sys.stderr,
            )
            sys.exit(1)
        base = Path(appdata) / "Cursor" / "User"
    else:
        print(
            f"Error: Unsupported platform '{system}'.\n"
            f"cursaves supports macOS, Linux, and Windows.\n"
            f"On macOS, Cursor data is at ~/Library/Application Support/Cursor/User/\n"
            f"On Linux, Cursor data is at ~/.config/Cursor/User/\n"
            f"On Windows, Cursor data is at %APPDATA%/Cursor/User/",
            file=sys.stderr,
        )
        sys.exit(1)

    if not base.exists():
        print(
            f"Error: Cursor data directory not found at:\n"
            f"  {base}\n\n"
            f"This usually means:\n"
            f"  - Cursor is not installed on this machine, or\n"
            f"  - Cursor has never been opened (no data created yet), or\n"
            f"  - Cursor stores data at a non-standard location\n\n"
            f"Expected path for {system}: {base}",
            file=sys.stderr,
        )
        sys.exit(1)

    return base


def get_global_db_path() -> Path:
    """Return the path to Cursor's global state.vscdb."""
    return get_cursor_user_dir() / "globalStorage" / "state.vscdb"


def get_workspace_storage_dir() -> Path:
    """Return the path to Cursor's workspace storage directory."""
    return get_cursor_user_dir() / "workspaceStorage"


def get_cursor_projects_dir() -> Path:
    """Return the path to ~/.cursor/projects/ (agent transcripts, etc.)."""
    return Path.home() / ".cursor" / "projects"


def sanitize_project_path(project_path: str) -> str:
    """Convert a project path to Cursor's sanitized directory name format.

    /Users/callum/Desktop/Projects/myrepo -> Users-callum-Desktop-Projects-myrepo
    C:\\Users\\ender\\Downloads\\cursaves-main -> c-Users-ender-Downloads-cursaves-main
    """
    normalized = os.path.normpath(os.path.expanduser(project_path))
    if os.name == "nt" and len(normalized) >= 2 and normalized[1] == ":":
        drive = normalized[0].lower()
        rest = normalized[2:].lstrip("\\/")
        parts = [drive] + [p for p in re.split(r"[\\/]+", rest) if p]
        return "-".join(parts)
    return normalized.strip("/").replace("/", "-")


def path_to_file_uri(path: str) -> str:
    """Convert a native filesystem path to a Cursor/VS Code file:// URI."""
    normalized = os.path.normpath(os.path.expanduser(path))
    if os.name == "nt" and len(normalized) >= 2 and normalized[1] == ":":
        drive = normalized[0].lower()
        rest = normalized[2:].replace("\\", "/").lstrip("/")
        encoded_rest = "/".join(quote(part, safe="") for part in rest.split("/"))
        return f"file:///{drive}%3A/{encoded_rest}" if encoded_rest else f"file:///{drive}%3A"
    posix_path = normalized.replace("\\", "/")
    if not posix_path.startswith("/"):
        posix_path = "/" + posix_path
    encoded = "/".join(quote(part, safe="") for part in posix_path.split("/"))
    return f"file://{encoded}"


def file_uri_to_path(uri: str) -> str:
    """Convert a file:// URI from workspace.json to a native filesystem path."""
    if not uri.startswith("file://"):
        raise ValueError(f"Not a file:// URI: {uri}")

    parsed = urlparse(uri)
    path = unquote(parsed.path or "")

    if os.name == "nt":
        # file:///c%3A/Users/... or file:///C:/Users/...
        if re.match(r"^/[a-zA-Z]:", path):
            path = path[1:]
        elif re.match(r"^/[a-zA-Z]%3A", path, re.IGNORECASE):
            path = unquote(path[1:])
        normalized = os.path.normpath(path.replace("/", "\\"))
        if len(normalized) >= 2 and normalized[1] == ":":
            normalized = normalized[0].upper() + normalized[1:]
        return normalized

    if path.startswith("//"):
        path = path[1:]
    return os.path.normpath(path)


def paths_equal(a: str, b: str) -> bool:
    """Compare two filesystem paths, case-insensitive on Windows."""
    a_norm = os.path.normpath(os.path.expanduser(a))
    b_norm = os.path.normpath(os.path.expanduser(b))
    if os.name == "nt":
        return os.path.normcase(a_norm) == os.path.normcase(b_norm)
    return a_norm == b_norm


def _decode_ssh_host(host: str) -> str:
    """Decode an SSH host identifier.

    Cursor encodes SSH hosts as hex-encoded JSON, e.g.:
    7b22686f73744e616d65223a22636f7265227d -> {"hostName":"core"} -> core
    """
    try:
        # Try to decode as hex
        decoded = bytes.fromhex(host).decode("utf-8")
        # Try to parse as JSON
        data = json.loads(decoded)
        if isinstance(data, dict) and "hostName" in data:
            return data["hostName"]
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    return host


def find_workspace_dirs_for_project(project_path: str) -> list[Path]:
    """Find all workspace directories that map to a given project path.

    Scans workspace.json files in workspaceStorage/ to find matches.
    Returns list of workspace directory paths, newest first.
    """
    ws_storage = get_workspace_storage_dir()
    if not ws_storage.exists():
        return []

    # Normalise the target path for comparison
    target = os.path.normpath(os.path.expanduser(project_path))

    matches = []
    for ws_dir in ws_storage.iterdir():
        if not ws_dir.is_dir():
            continue
        ws_json = ws_dir / "workspace.json"
        if not ws_json.exists():
            continue
        try:
            data = json.loads(ws_json.read_text())
            folder_uri = data.get("folder", "")
            # Handle file:// URIs
            if folder_uri.startswith("file://"):
                folder_path = file_uri_to_path(folder_uri)
            elif folder_uri.startswith("vscode-remote://"):
                # SSH remote workspace - extract the path portion
                # Format: vscode-remote://ssh-remote%2B<host>/<path>
                parts = folder_uri.split("/", 3)
                if len(parts) >= 4:
                    folder_path = "/" + parts[3]
                else:
                    continue
            else:
                continue

            if paths_equal(folder_path, target):
                matches.append(ws_dir)
        except (json.JSONDecodeError, OSError):
            continue

    # Sort by modification time, newest first
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches


def find_transcript_dir(project_path: str) -> Optional[Path]:
    """Find the agent-transcripts directory for a project."""
    projects_dir = get_cursor_projects_dir()
    if not projects_dir.exists():
        return None

    sanitized = sanitize_project_path(project_path)
    transcript_dir = projects_dir / sanitized / "agent-transcripts"
    if transcript_dir.exists():
        return transcript_dir

    return None


def get_project_path() -> str:
    """Get the current project path (current working directory)."""
    return os.getcwd()


def list_all_workspaces() -> list[dict]:
    """List all Cursor workspaces with metadata.

    Returns a list of dicts with:
      - folder_uri: raw URI from workspace.json
      - path: extracted filesystem path (for workspace, path to the .code-workspace file)
      - type: 'local', 'ssh', or 'workspace'
      - host: SSH hostname (for ssh type, None otherwise)
      - workspace_dir: Path to the workspace directory
      - mtime: modification time of the workspace DB
    """
    ws_storage = get_workspace_storage_dir()
    if not ws_storage.exists():
        return []

    workspaces = []
    for ws_dir in ws_storage.iterdir():
        if not ws_dir.is_dir():
            continue
        ws_json = ws_dir / "workspace.json"
        if not ws_json.exists():
            continue
        try:
            data = json.loads(ws_json.read_text())

            ws_type = "local"
            host = None
            folder_path = ""
            folder_uri = ""

            # workspace .code-workspace: uses "workspace" key instead of "folder"
            if "workspace" in data and not data.get("folder"):
                ws_uri = data["workspace"]
                if ws_uri.startswith("file://"):
                    folder_uri = ws_uri
                    folder_path = file_uri_to_path(ws_uri)
                    ws_type = "workspace"
                else:
                    continue
            else:
                folder_uri = data.get("folder", "")
                if not folder_uri:
                    continue

                if folder_uri.startswith("file://"):
                    folder_path = file_uri_to_path(folder_uri)
                elif folder_uri.startswith("vscode-remote://"):
                    ws_type = "ssh"
                    # Format: vscode-remote://ssh-remote%2B<host>/<path>
                    authority = folder_uri.split("/")[2]  # ssh-remote%2B<host>
                    if "%2B" in authority:
                        host = authority.split("%2B", 1)[1]
                    elif "+" in authority:
                        host = authority.split("+", 1)[1]
                    # Decode the host if it's hex-encoded JSON (e.g. {"hostName":"core"})
                    if host:
                        host = _decode_ssh_host(host)
                    parts = folder_uri.split("/", 3)
                    if len(parts) >= 4:
                        folder_path = "/" + parts[3]
                    else:
                        continue
                else:
                    continue

            # Get DB modification time
            db_path = ws_dir / "state.vscdb"
            mtime = db_path.stat().st_mtime if db_path.exists() else 0

            workspaces.append({
                "folder_uri": folder_uri,
                "path": os.path.normpath(folder_path),
                "type": ws_type,
                "host": host,
                "workspace_dir": ws_dir,
                "mtime": mtime,
            })
        except (json.JSONDecodeError, OSError):
            continue

    # Sort by modification time, newest first
    workspaces.sort(key=lambda w: w["mtime"], reverse=True)
    return workspaces


def get_global_composer_headers() -> list[dict]:
    """Read the central composer.composerHeaders from the global DB.

    Returns the allComposers list from composer.composerHeaders in the
    global DB's ItemTable. In Cursor 3.0+ this is the authoritative
    index of all chats, each tagged with a workspaceIdentifier.

    Returns an empty list if not present (pre-3.0 Cursor).
    """
    from . import db

    global_db = get_global_db_path()
    if not global_db.exists():
        return []
    try:
        with db.CursorDB(global_db) as cdb:
            headers = cdb.get_json("composer.composerHeaders", table="ItemTable")
            if headers and isinstance(headers, dict):
                return headers.get("allComposers", [])
    except Exception:
        pass
    return []


# Sentinel workspace id for chats that carry no workspaceIdentifier in the
# global index. Newer Cursor builds leave many chats untagged; they still
# appear in the (global) agents window, so we surface them under this bucket.
UNASSIGNED_WS_ID = "unassigned"

_global_headers_cache: Optional[dict[str, list[dict]]] = None


def _build_global_headers_map() -> dict[str, list[dict]]:
    """Build a workspace-hash → [composer header entries] map from the global index.

    Returns a dict keyed by workspace directory hash (workspaceIdentifier.id).
    Each value is a list of composer header dicts for that workspace.

    Two sources are merged (deduplicated by composerId within each workspace):

      1. The legacy ``composer.composerHeaders`` ItemTable blob (pre-migration
         Cursor 3.0 builds keep the whole index here).
      2. The per-row ``composerData:*`` entries in the global DB's
         ``cursorDiskKV`` table. Newer Cursor builds migrate the header index
         out of the ItemTable blob into these rows (flagged by
         ``composer.composerHeaders.migratedToTable``), leaving the blob empty.
         Without this source cursaves is blind to every chat that only lives
         in the migrated table.

    Chats whose ``workspaceIdentifier`` is missing are grouped under the
    ``UNASSIGNED_WS_ID`` sentinel so they stay discoverable. Cached for the
    lifetime of the process.
    """
    global _global_headers_cache
    if _global_headers_cache is not None:
        return _global_headers_cache

    from . import db

    result: dict[str, list[dict]] = {}
    seen_by_ws: dict[str, set[str]] = {}

    def _add(entry: dict) -> None:
        cid = entry.get("composerId")
        if not cid:
            return
        wi = entry.get("workspaceIdentifier")
        ws_id = wi.get("id") if isinstance(wi, dict) else None
        ws_id = ws_id or UNASSIGNED_WS_ID
        seen = seen_by_ws.setdefault(ws_id, set())
        if cid in seen:
            return
        seen.add(cid)
        result.setdefault(ws_id, []).append(entry)

    global_db = get_global_db_path()
    if global_db.exists():
        try:
            with db.CursorDB(global_db) as cdb:
                # Source 1: legacy ItemTable headers blob
                blob = cdb.get_json("composer.composerHeaders", table="ItemTable")
                if isinstance(blob, dict):
                    for entry in blob.get("allComposers", []):
                        if isinstance(entry, dict):
                            _add(entry)

                # Source 2: migrated per-row composerData entries
                for key in cdb.list_keys("composerData:", table="cursorDiskKV"):
                    cd = cdb.get_json(key, table="cursorDiskKV")
                    if not isinstance(cd, dict):
                        continue
                    _add({
                        "composerId": key[len("composerData:"):],
                        "name": cd.get("name", ""),
                        "createdAt": cd.get("createdAt", 0),
                        "lastUpdatedAt": cd.get("lastUpdatedAt", 0),
                        "unifiedMode": cd.get("unifiedMode", "agent"),
                        "forceMode": cd.get("forceMode", ""),
                        "workspaceIdentifier": cd.get("workspaceIdentifier"),
                    })
        except Exception:
            pass

    _global_headers_cache = result
    return result


def invalidate_headers_cache():
    """Clear the cached global headers map (call after writing to the global DB)."""
    global _global_headers_cache
    _global_headers_cache = None


def get_workspace_composer_ids(ws_db_path: Path) -> list[str]:
    """Extract all composer IDs associated with a workspace.

    Combines multiple sources for maximum coverage:
    1. Global composer.composerHeaders index (Cursor 3.0+, most authoritative
       but only contains recently-active chats)
    2. Workspace DB selectedComposerIds + composerChatViewPane entries
       (catches chats opened before the 3.0 migration that aren't yet
       in the global index)
    3. Workspace DB allComposers (Cursor 2.x fallback)

    Returns deduplicated IDs.
    """
    from . import db

    ids: set[str] = set()
    ws_hash = ws_db_path.parent.name

    # Source 1: global headers index (Cursor 3.0+)
    headers_map = _build_global_headers_map()
    for entry in headers_map.get(ws_hash, []):
        cid = entry.get("composerId")
        if cid:
            ids.add(cid)

    # Source 2+3: workspace DB
    try:
        with db.CursorDB(ws_db_path) as cdb:
            data = cdb.get_json("composer.composerData", table="ItemTable")
            if not data:
                return list(ids)

            # Cursor 2.x: allComposers (complete list for old workspaces)
            for c in data.get("allComposers", []):
                cid = c.get("composerId")
                if cid:
                    ids.add(cid)

            # Cursor 3.0+: supplementary sources for chats not in global index
            for cid in data.get("selectedComposerIds", []):
                if cid:
                    ids.add(cid)
            for cid in data.get("lastFocusedComposerIds", []):
                if cid:
                    ids.add(cid)

            for key in cdb.list_keys(
                "workbench.panel.composerChatViewPane.", table="ItemTable"
            ):
                pane = cdb.get_json(key, table="ItemTable")
                if isinstance(pane, dict):
                    for view_key in pane:
                        if ".view." in view_key:
                            cid = view_key.rsplit(".", 1)[-1]
                            if cid:
                                ids.add(cid)
    except Exception:
        pass

    return list(ids)


def list_workspaces_with_conversations() -> list[dict]:
    """List workspaces that have at least one conversation.

    Returns the same dicts as list_all_workspaces(), plus a
    'conversations' key with the count.
    """
    headers_map = _build_global_headers_map()
    result = []
    covered_ws_ids: set[str] = set()

    for ws in list_all_workspaces():
        ws_hash = ws["workspace_dir"].name
        db_path = ws["workspace_dir"] / "state.vscdb"
        if db_path.exists():
            composer_ids = get_workspace_composer_ids(db_path)
        else:
            # No local DB, but the global index may still tag chats to this hash.
            composer_ids = [
                e.get("composerId")
                for e in headers_map.get(ws_hash, [])
                if e.get("composerId")
            ]
        if composer_ids:
            covered_ws_ids.add(ws_hash)
            ws["conversations"] = len(composer_ids)
            result.append(ws)

    # Global-index workspace hashes that have no workspaceStorage dir (e.g. the
    # dir was removed, or Cursor changed its hashing across versions). Surface
    # them so their chats remain listable/targetable by hash.
    for ws_id, entries in headers_map.items():
        if ws_id == UNASSIGNED_WS_ID or ws_id in covered_ws_ids:
            continue
        count = len([e for e in entries if e.get("composerId")])
        if not count:
            continue
        result.append({
            "folder_uri": "",
            "path": f"(no workspace dir: {ws_id[:12]})",
            "type": "global",
            "host": None,
            "workspace_dir": get_workspace_storage_dir() / ws_id,
            "mtime": 0,
            "conversations": count,
        })

    # Synthetic bucket for chats that carry no workspaceIdentifier at all.
    unassigned = [e for e in headers_map.get(UNASSIGNED_WS_ID, []) if e.get("composerId")]
    if unassigned:
        result.append({
            "folder_uri": "",
            "path": "(global / unassigned)",
            "type": "global",
            "host": None,
            "workspace_dir": get_workspace_storage_dir() / UNASSIGNED_WS_ID,
            "mtime": 0,
            "conversations": len(unassigned),
        })

    return result


def resolve_workspace(selector: str) -> Optional[dict]:
    """Resolve a workspace selector to a workspace dict.

    The selector can be:
      - A number (1-based index from list_workspaces_with_conversations)
      - A workspace hash (directory name under workspaceStorage/)
      - A path substring (matched against workspace paths)
    """
    workspaces = list_workspaces_with_conversations()

    # Try as index
    try:
        idx = int(selector)
        if workspaces and 1 <= idx <= len(workspaces):
            return workspaces[idx - 1]
        return None
    except ValueError:
        pass

    # Try as workspace hash. Exact match wins; otherwise allow a prefix match
    # for hash-length selectors (>= 8 chars) so users can paste either the
    # short 8-char hash shown in `cursaves workspaces` or any longer prefix,
    # e.g. `cursaves push -w 497e8ab0` or `-w f62e124ab`.
    for ws in workspaces:
        if ws["workspace_dir"].name == selector:
            return ws
    if len(selector) >= 8:
        prefix_matches = [
            ws for ws in workspaces
            if ws["workspace_dir"].name.startswith(selector)
        ]
        if len(prefix_matches) == 1:
            return prefix_matches[0]
        if len(prefix_matches) > 1:
            return None  # ambiguous - refuse rather than guess

    # Try as path substring
    for ws in workspaces:
        if selector in ws["path"]:
            return ws

    return None


def get_sync_dir() -> Path:
    """Return the cursaves sync directory (~/.cursaves/).

    This is the git repo that holds snapshots and is synced between machines.
    """
    return Path.home() / ".cursaves"


def get_snapshots_dir() -> Path:
    """Return the snapshots directory (~/.cursaves/snapshots/)."""
    snapshots = get_sync_dir() / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    return snapshots


def get_cursor_dot_dir() -> Path:
    """Return ~/.cursor/ (user-level Cursor config, skills, hooks, etc.)."""
    return Path.home() / ".cursor"


def get_profile_staging_dir() -> Path:
    """Return the profile staging directory (~/.cursaves/profile/)."""
    profile = get_sync_dir() / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def is_sync_repo_initialized() -> bool:
    """Check if a sync backend has been configured (git repo or cloud)."""
    sync_dir = get_sync_dir()
    if (sync_dir / ".git").exists():
        return True
    # Check for non-git backend config
    config_path = Path.home() / ".config" / "cursaves" / "config.json"
    if config_path.exists():
        try:
            import json
            cfg = json.loads(config_path.read_text())
            return cfg.get("backend") in ("s3", "azure")
        except Exception:
            pass
    return False


def get_machine_id() -> str:
    """Return a human-readable machine identifier."""
    import socket

    return socket.gethostname()


# ── Workspace matching for imports ─────────────────────────────────────


def find_all_matching_workspaces(source_path: str) -> list[dict]:
    """Find all workspaces that could receive imports from source_path.

    Matches by:
    1. Exact path match (for SSH workspaces with same remote path)
    2. Same basename (fallback for different directory structures)

    Returns list of workspace dicts with type, host, path, workspace_dir,
    sorted by match quality (exact matches first) then by mtime.
    """
    all_ws = list_all_workspaces()
    source_normalized = os.path.normpath(source_path)
    source_basename = os.path.basename(source_normalized)

    exact_matches = []
    basename_matches = []

    for ws in all_ws:
        ws_path = ws["path"]
        ws_basename = os.path.basename(ws_path)

        if paths_equal(ws_path, source_normalized):
            exact_matches.append(ws)
        elif ws_basename == source_basename:
            basename_matches.append(ws)

    # Return exact matches first, then basename matches
    return exact_matches + basename_matches


def format_workspace_display(ws: dict, include_path: bool = True) -> str:
    """Format a workspace dict for display.

    Returns a string like "ssh core /mnt/home/.../project", "(local) /home/.../project",
    or "(workspace) /home/.../my-proj.code-workspace"
    """
    if ws["type"] == "ssh":
        host = ws.get("host") or "unknown"
        if include_path:
            path = ws["path"]
            if len(path) > 40:
                path = "..." + path[-37:]
            return f"ssh {host} {path}"
        return f"ssh {host}"
    elif ws["type"] == "workspace":
        if include_path:
            path = ws["path"]
            if len(path) > 45:
                path = "..." + path[-42:]
            return f"(workspace) {path}"
        return "(workspace)"
    else:
        if include_path:
            path = ws["path"]
            if len(path) > 45:
                path = "..." + path[-42:]
            return f"(local) {path}"
        return "(local)"


# ── Project identification ────────────────────────────────────────────


def get_project_identifier(project_path: str) -> str:
    """Get a stable identifier for a project, used as the snapshot subdirectory.

    Uses the git remote origin URL if available (normalized to a filesystem-safe
    string).  Falls back to the directory basename for non-git projects.

    This means:
      - Same repo under different local names (bob/ vs alice/) → same identifier
      - Different repos that happen to share a name → different identifiers
    """
    remote_url = _get_git_remote_url(project_path)
    if remote_url:
        return _normalize_remote_url(remote_url)
    return os.path.basename(os.path.normpath(project_path))


def _get_git_remote_url(project_path: str) -> Optional[str]:
    """Get the git remote origin URL for a project, if any."""
    try:
        result = subprocess.run(
            ["git", "-C", project_path, "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _normalize_remote_url(url: str) -> str:
    """Normalize a git remote URL to a stable, filesystem-safe directory name.

    git@github.com:user/repo.git     → github.com-user-repo
    https://github.com/user/repo.git → github.com-user-repo
    ssh://git@github.com/user/repo   → github.com-user-repo
    """
    # Strip trailing .git
    url = re.sub(r"\.git$", "", url)

    # SSH shorthand: git@host:user/repo
    m = re.match(r"^[\w.-]+@([\w.-]+):(.*)", url)
    if m:
        host, path = m.group(1), m.group(2)
        return _sanitize_identifier(f"{host}/{path}")

    # HTTPS / SSH URI: https://host/path or ssh://git@host/path
    m = re.match(r"^(?:https?|ssh)://(?:[\w.-]+@)?([\w.-]+)/(.*)", url)
    if m:
        host, path = m.group(1), m.group(2)
        return _sanitize_identifier(f"{host}/{path}")

    # Unknown format -- sanitize whatever we got
    return _sanitize_identifier(url)


def _sanitize_identifier(s: str) -> str:
    """Turn an arbitrary string into a safe directory name.

    Replaces slashes, colons, @, etc. with '-' and collapses runs of dashes.
    """
    s = re.sub(r"[/:@\\]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")
