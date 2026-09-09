"""CLI entry point for cursaves."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import __version__, db, export, paths, profile, setup_wizard
from . import github_auth
from .backends import (
    GitBackend,
    S3Backend,
    SyncBackend,
    apply_git_identity,
    get_backend,
    get_git_config,
    load_config,
    read_global_git_identity,
    save_config,
    save_git_config,
    set_git_verbose,
)
from .importer import (
    copy_between_workspaces,
    doctor_audit,
    doctor_recover,
    find_snapshot_dir_for_project,
    format_sync_status,
    get_push_status_for_conversation,
    get_sync_status_for_snapshot,
    import_all_snapshots,
    import_from_snapshot_dir,
    import_snapshot,
    list_snapshot_projects,
    list_snapshot_files,
    read_snapshot_file,
    read_snapshot_meta,
    repair_missing_blobs,
)


def _get_snapshot_id(path: Path) -> str:
    """Extract the snapshot ID (composer ID) from a snapshot filename."""
    name = path.name
    if name.endswith(".json.gz"):
        return name[:-8]
    elif name.endswith(".json"):
        return name[:-5]
    return path.stem


def _delete_snapshot(path: Path):
    """Delete a snapshot file (or its shards) and metadata sidecar."""
    sid = _get_snapshot_id(path)
    if path.exists():
        path.unlink()
    # Remove any shard files (*.json.gz.00, .01, ...)
    for shard in path.parent.glob(f"{sid}.json.gz.*"):
        if not shard.name.endswith(".meta.json"):
            shard.unlink()
    meta = path.parent / f"{sid}.meta.json"
    if meta.exists():
        meta.unlink()
from .reload import print_reload_hint
from .hook_install import install_watch_hook, uninstall_watch_hook
from .watch import detach_watch_all, watch_all_loop, POLL_INTERVAL_DEFAULT



def _ensure_synced() -> None:
    """Pull latest from remote to ensure we have the latest state."""
    if paths.is_sync_repo_initialized():
        backend = get_backend()
        snapshots_dir = paths.get_snapshots_dir()
        if backend.has_remote():
            backend.pull(snapshots_dir)


def _resolve_project(args) -> str:
    """Resolve the project path from --workspace, --project, or cwd."""
    if hasattr(args, "workspace") and args.workspace:
        ws = paths.resolve_workspace(args.workspace)
        if ws is None:
            print(
                f"Error: No workspace matching '{args.workspace}'.\n"
                f"Run 'cursaves workspaces' to see available workspaces.",
                file=sys.stderr,
            )
            sys.exit(1)
        return ws["path"]
    return args.project if (hasattr(args, "project") and args.project) else paths.get_project_path()


def _resolve_project_and_workspace(args) -> tuple[str, "Path | None", str | None]:
    """Resolve project path, workspace_dir, and host from --workspace, --project, or cwd.

    When -w is used, returns the specific workspace_dir so operations
    are scoped to that exact workspace (prevents cross-host contamination
    for SSH workspaces with the same remote path).
    """
    if hasattr(args, "workspace") and args.workspace:
        ws = paths.resolve_workspace(args.workspace)
        if ws is None:
            print(
                f"Error: No workspace matching '{args.workspace}'.\n"
                f"Run 'cursaves workspaces' to see available workspaces.",
                file=sys.stderr,
            )
            sys.exit(1)
        return ws["path"], ws["workspace_dir"], ws.get("host")
    project = args.project if (hasattr(args, "project") and args.project) else paths.get_project_path()
    return project, None, None


def _resolve_workspace_for_import(args) -> tuple[str, "Path | None"]:
    """Resolve the project path and optional workspace directory for import.

    When -w is specified, returns (project_path, workspace_dir) so imports go
    directly into that specific workspace. Otherwise returns (project_path, None)
    and the importer will find/create a workspace automatically.
    """
    from pathlib import Path

    if hasattr(args, "workspace") and args.workspace:
        ws = paths.resolve_workspace(args.workspace)
        if ws is None:
            print(
                f"Error: No workspace matching '{args.workspace}'.\n"
                f"Run 'cursaves workspaces' to see available workspaces.",
                file=sys.stderr,
            )
            sys.exit(1)
        return ws["path"], ws["workspace_dir"]

    project_path = args.project if (hasattr(args, "project") and args.project) else paths.get_project_path()
    return project_path, None


def _workspace_sync_summary(ws: dict, _global_cdb: "Optional[db.CursorDB]" = None) -> str:
    """Compute a short sync summary for a workspace.

    Reads the workspace's conversations and checks each against snapshots.
    Pass _global_cdb to avoid re-copying the global DB per workspace.
    Returns a string like "3 synced, 2 not pushed" or "5 synced".
    """
    ws_dir = ws["workspace_dir"]
    db_path = ws_dir / "state.vscdb"
    if not db_path.exists():
        return ""

    composer_ids = paths.get_workspace_composer_ids(db_path)
    if not composer_ids:
        return ""

    project_id = paths.get_project_identifier(ws["path"])

    counts = {"up_to_date": 0, "local_ahead": 0, "behind": 0, "never_pushed": 0}
    for cid in composer_ids:
        status = get_push_status_for_conversation(cid, project_id, _cdb=_global_cdb)
        counts[status] = counts.get(status, 0) + 1

    parts = []
    if counts["up_to_date"]:
        parts.append(f"{counts['up_to_date']} synced")
    if counts["local_ahead"]:
        parts.append(f"{counts['local_ahead']} ahead")
    if counts["behind"]:
        parts.append(f"{counts['behind']} behind")
    if counts["never_pushed"]:
        parts.append(f"{counts['never_pushed']} not pushed")

    return ", ".join(parts) if parts else ""


def cmd_workspaces(args):
    """List Cursor workspaces that have conversations."""
    workspaces = paths.list_workspaces_with_conversations()
    if not workspaces:
        print("No workspaces with conversations found.")
        return

    print(f"{'#':<4} {'Type':<10} {'Path':<38} {'Host':<12} {'Chats':>5}  {'Hash':<9}  {'Sync Status'}")
    print("-" * 115)

    global_db_path = paths.get_global_db_path()
    global_cdb = db.CursorDB(global_db_path) if global_db_path.exists() else None
    try:
        for i, ws in enumerate(workspaces, 1):
            path = ws["path"]
            if len(path) > 36:
                path = "..." + path[-33:]
            host = ws["host"] or ""
            convos = ws.get("conversations", 0)
            sync = _workspace_sync_summary(ws, _global_cdb=global_cdb)
            ws_hash = ws["workspace_dir"].name[:8]

            print(f"{i:<4} {ws['type']:<10} {path:<38} {host:<12} {convos:>5}  {ws_hash}  {sync}")
    finally:
        if global_cdb:
            global_cdb.close()

    print(f"\n{len(workspaces)} workspace(s) with conversations")
    print("\nUse 'cursaves push -w <number or hash>' to push a specific workspace.")


def _is_remote_path(path: str, source_machine: str) -> bool:
    """Check if a path looks like it came from an SSH remote session."""
    import platform

    # If path doesn't exist locally, it's likely remote
    if not os.path.exists(path):
        return True

    system = platform.system()

    # On Mac, local paths start with /Users
    if system == "Darwin" and not path.startswith("/Users"):
        return True

    # On Linux, local paths are Unix-style; Windows-style paths are remote
    if system == "Linux" and re.match(r"^[a-zA-Z]:[\\/]", path):
        return True

    # On Windows, Unix-style paths are remote
    if system == "Windows" and (path.startswith("/home/") or path.startswith("/Users/")):
        return True

    return False


def cmd_snapshots(args):
    """List all snapshot projects available in ~/.cursaves/snapshots/."""
    _ensure_synced()  # Pull latest from remote first
    snapshots_dir = paths.get_snapshots_dir()
    projects = list_snapshot_projects(snapshots_dir)

    if not projects:
        print("No snapshots found in ~/.cursaves/snapshots/")
        print("Run 'cursaves push' to checkpoint and push conversations.")
        return

    global_db_path = paths.get_global_db_path()
    global_cdb = db.CursorDB(global_db_path) if global_db_path.exists() else None
    try:
        for i, p in enumerate(projects, 1):
            name = p["name"]
            print(f"\n  {name}/ ({p['count']} snapshot(s))")

            snapshot_files = list_snapshot_files(p["path"])
            for sf in snapshot_files:
                meta = read_snapshot_meta(sf)
                chat_name = meta.get("name") or "Untitled"
                msgs = meta.get("messageCount", 0)
                exported = (meta.get("exportedAt") or "")[:16] or "unknown"
                source_host = meta.get("sourceHost")
                source = source_host or meta.get("sourceMachine") or "unknown"
                cid = meta.get("composerId")
                if cid:
                    status = get_sync_status_for_snapshot(cid, msgs, _cdb=global_cdb)
                    status_label = f"[{format_sync_status(status)}]"
                else:
                    status_label = ""

                if len(chat_name) > 36:
                    chat_name = chat_name[:33] + "..."
                print(f"    {chat_name:<38} {msgs:>5} msgs  from {source:<16} {status_label}")
    finally:
        if global_cdb:
            global_cdb.close()

    print(f"\n{len(projects)} project(s) with snapshots")
    print(f"\nUse 'cursaves pull -s' to interactively select which to import.")


def cmd_auth_github(args):
    """Login with GitHub — auth, commit identity, and sync remote."""
    if getattr(args, "status", False):
        github_auth.print_auth_status()
        return
    if getattr(args, "logout", False):
        github_auth.logout()
        return
    if not github_auth.find_gh():
        yes = getattr(args, "yes", False)
        if github_auth.can_auto_install_gh():
            do_install = yes
            if not yes and not getattr(args, "remote", None):
                from .interactive import confirm
                do_install = confirm(
                    "GitHub CLI (gh) not found. Install now?\n"
                    f"  {github_auth.gh_auto_install_description()}",
                    default=True,
                )
            if do_install:
                ok, msg = github_auth.install_gh()
                print(msg)
                if not ok or not github_auth.find_gh():
                    sys.exit(1)
            else:
                github_auth.ensure_gh()
        else:
            github_auth.ensure_gh()
    github_auth.run_auth_flow(
        login_only=getattr(args, "login_only", False),
        remote_url=getattr(args, "remote", None),
        create_repo=getattr(args, "create_repo", False),
        yes=getattr(args, "yes", False),
        interactive=not getattr(args, "yes", False) and not getattr(args, "remote", None),
    )


def cmd_auth(args):
    """Authentication subcommands."""
    if args.auth_command == "github":
        cmd_auth_github(args)
    else:
        print("Error: unknown auth command.", file=sys.stderr)
        sys.exit(1)


def cmd_init(args):
    """Initialize cursaves sync — git repo or S3 bucket."""
    sync_dir = paths.get_sync_dir()
    snapshots_dir = sync_dir / "snapshots"
    backend_type = getattr(args, "backend", None) or "git"

    if backend_type == "s3":
        bucket = getattr(args, "bucket", None)
        if not bucket:
            print("Error: --bucket is required for S3 backend.", file=sys.stderr)
            print("  cursaves init --backend s3 --bucket my-cursor-saves", file=sys.stderr)
            sys.exit(1)

        snapshots_dir.mkdir(parents=True, exist_ok=True)
        (sync_dir / "profile").mkdir(parents=True, exist_ok=True)

        config = load_config()
        config["backend"] = "s3"
        config.setdefault("s3", {})
        config.setdefault("profile", {
            "enabled": True,
            "categories": dict(profile.DEFAULT_CATEGORIES),
        })
        config["s3"]["bucket"] = bucket
        if getattr(args, "prefix", None):
            config["s3"]["prefix"] = args.prefix
        if getattr(args, "region", None):
            config["s3"]["region"] = args.region
        save_config(config)

        backend = S3Backend(
            bucket=bucket,
            prefix=config["s3"].get("prefix", "snapshots/"),
            region=config["s3"].get("region"),
        )

        print(f"Configured S3 backend:")
        print(f"  Bucket: {bucket}")
        print(f"  Prefix: {config['s3'].get('prefix', 'snapshots/')}")
        if config["s3"].get("region"):
            print(f"  Region: {config['s3']['region']}")
        print(f"  Snapshots: {snapshots_dir}")

        # Verify access
        try:
            if backend.is_initialized():
                print(f"\n  Bucket access verified.")
            else:
                print(f"\n  Warning: Could not access bucket '{bucket}'.", file=sys.stderr)
                print(f"  Check your AWS credentials and bucket permissions.", file=sys.stderr)
        except Exception as e:
            print(f"\n  Warning: Could not verify bucket access: {e}", file=sys.stderr)

        print(f"\nDone. Run 'cursaves sync' to synchronize conversations.")
        return

    # Git backend (default / backward-compatible)
    remote = args.remote or load_config().get("github", {}).get("remote_url")
    if remote:
        remote = github_auth.normalize_to_https(remote)

    if paths.is_sync_repo_initialized():
        config = load_config()
        if config.get("backend") == "s3":
            print(f"Currently configured with S3 backend (bucket: {config.get('s3', {}).get('bucket')})")
            if args.remote:
                print("Switching to git backend...")
                config["backend"] = "git"
                save_config(config)
            else:
                return

        git_backend = GitBackend(sync_dir)
        print(f"Sync repo already initialized at {sync_dir}")
        if remote:
            git_backend.update_remote(remote)
            print(f"  Remote updated: {remote}")
        _save_git_identity_from_args(args)
        if paths.is_sync_repo_initialized():
            apply_git_identity(sync_dir)
        return

    print(f"Initializing sync repo at {sync_dir}...")
    _save_git_identity_from_args(args)
    git_backend = GitBackend(sync_dir)
    git_backend.init_repo(remote=remote)
    _ensure_profile_config()
    print(f"  Created {sync_dir}")

    if remote:
        print(f"  Added remote: {remote}")
        print(f"\nDone. Run 'cursaves push' from any project directory to start syncing.")
    else:
        print(f"\nDone. To sync between machines, run:")
        print(f"  cursaves auth github")
        print(f"  cursaves init --backend s3 --bucket my-cursor-saves")


def cmd_list(args):
    """List conversations for the current project."""
    project_path, workspace_dir, _ = _resolve_project_and_workspace(args)
    conversations = export.list_conversations(project_path, workspace_dir=workspace_dir)

    if not conversations:
        print(f"No conversations found for {project_path}", file=sys.stderr)
        ws_dirs = paths.find_workspace_dirs_for_project(project_path)
        if not ws_dirs:
            print(
                f"\nNo Cursor workspace found for this path. Possible causes:\n"
                f"  - This directory has never been opened in Cursor\n"
                f"  - The path doesn't match exactly (try an absolute path with -p)\n"
                f"  - Cursor data is in a non-standard location",
                file=sys.stderr,
            )
        else:
            print("(Workspace found but contains no conversations.)", file=sys.stderr)
        return

    # JSON output mode
    if args.json:
        print(json.dumps(conversations, indent=2))
        return

    print(f"Conversations for {project_path}\n")
    print(f"{'ID':<40} {'Name':<30} {'Mode':<8} {'Msgs':>5}  {'Last Updated'}")
    print("-" * 110)

    for c in conversations:
        name = c["name"]
        if len(name) > 28:
            name = name[:25] + "..."
        print(
            f"{c['id']:<40} {name:<30} {c['mode']:<8} {c['messageCount']:>5}  {c['lastUpdated']}"
        )

    print(f"\n{len(conversations)} conversation(s) total")


def cmd_export(args):
    """Export a single conversation to a snapshot file."""
    project_path = _resolve_project(args)
    composer_id = args.id

    print(f"Exporting conversation {composer_id}...")
    snapshot = export.export_conversation(project_path, composer_id)

    if snapshot is None:
        print(f"Error: Conversation '{composer_id}' not found.", file=sys.stderr)
        sys.exit(1)

    snapshots_dir = paths.get_snapshots_dir()
    saved_path = export.save_snapshot(snapshot, snapshots_dir)
    print(f"Saved to {saved_path}")

    # Show summary
    data = snapshot["composerData"]
    headers = data.get("fullConversationHeadersOnly", [])
    blobs = snapshot.get("contentBlobs", {})
    print(f"  Messages: {len(headers)}")
    print(f"  Content blobs: {len(blobs)}")
    print(f"  Source: {snapshot['sourceMachine']}")


def cmd_checkpoint(args):
    """Checkpoint all conversations for the current project."""
    project_path, workspace_dir, _ = _resolve_project_and_workspace(args)

    print(f"Checkpointing conversations for {project_path}...")
    saved = export.checkpoint_project(project_path, workspace_dir=workspace_dir)

    if not saved:
        print("No conversations found to checkpoint.")
        return

    print(f"\nCheckpointed {len(saved)} conversation(s):")
    for p in saved:
        print(f"  {p}")

    print(f"\nSnapshots saved to {paths.get_snapshots_dir()}")
    print("Run 'git add snapshots/ && git commit -m \"checkpoint\"' to commit.")


def cmd_import(args):
    """Import conversation snapshots."""
    project_path = _resolve_project(args)

    if args.all:
        print(f"Importing all snapshots for {project_path}...")
        success, failure = import_all_snapshots(
            project_path,
            force=args.force,
        )
        print(f"\nDone: {success} imported, {failure} failed.")
        if success > 0:
            _maybe_reload(args)
    elif args.file:
        snapshot_path = Path(args.file)
        if not snapshot_path.exists():
            print(f"Error: File not found: {snapshot_path}", file=sys.stderr)
            sys.exit(1)
        print(f"Importing {snapshot_path.name}...")
        if import_snapshot(snapshot_path, project_path):
            print("Done.")
            _maybe_reload(args)
        else:
            print("Import failed.", file=sys.stderr)
            sys.exit(1)
    else:
        print("Error: Specify --all or --file <path>", file=sys.stderr)
        sys.exit(1)


def _select_target_workspaces(source_paths: set[str]) -> list[dict]:
    """Find and optionally prompt user to select target workspaces for import.

    Args:
        source_paths: Set of source project paths from snapshots.

    Returns:
        List of workspace dicts to import into, or empty list if cancelled.
        Each dict has: type, host, path, workspace_dir
    """
    # Find all matching workspaces across all source paths
    all_matches = []
    seen_ws_dirs = set()
    for sp in sorted(source_paths):
        matches = paths.find_all_matching_workspaces(sp)
        for ws in matches:
            ws_dir_str = str(ws["workspace_dir"])
            if ws_dir_str not in seen_ws_dirs:
                seen_ws_dirs.add(ws_dir_str)
                all_matches.append(ws)

    if not all_matches:
        return []

    if len(all_matches) == 1:
        # Single match - use it directly
        ws = all_matches[0]
        display = paths.format_workspace_display(ws)
        print(f"  Target workspace: {display}")
        return [ws]

    # Multiple matches - ask user to select
    print(f"\n  Multiple workspaces match this project:")
    print(f"  {'#':<4} {'Type':<6} {'Host':<15} {'Path'}")
    print(f"  {'-' * 70}")

    for i, ws in enumerate(all_matches, 1):
        host = ws.get("host") or ""
        ws_path = ws["path"]
        if len(ws_path) > 45:
            ws_path = "..." + ws_path[-42:]
        print(f"  {i:<4} {ws['type']:<6} {host:<15} {ws_path}")

    print(f"\n  Select workspace(s) to import into (e.g. 1,2 or 'all'):")
    try:
        choice = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return []

    if not choice:
        return []

    indices = _parse_selection(choice, len(all_matches))
    if not indices:
        return []

    return [all_matches[i - 1] for i in indices]


def _maybe_reload(args):
    """Print restart hint after import."""
    print_reload_hint()


def cmd_reload(args):
    """Print restart instructions."""
    print_reload_hint()


def _require_sync_repo():
    """Check that the sync repo is initialized, exit with help if not.

    Returns the sync directory path (for backward compat with existing callers).
    """
    if not paths.is_sync_repo_initialized():
        print(
            "Error: Sync repo not initialized.\n"
            "Run 'cursaves init' first to set up ~/.cursaves/\n\n"
            "Examples:\n"
            "  cursaves init --remote git@github.com:you/my-cursaves.git\n"
            "  cursaves init --backend s3 --bucket my-cursor-saves",
            file=sys.stderr,
        )
        sys.exit(1)
    return paths.get_sync_dir()


def _parse_selection(choice: str, max_items: int) -> list[int]:
    """Parse a user selection string into a list of 1-based indices.

    Supports: 1,3,5 and 1-3 and combinations like 1-3,5 and 'all'.
    Returns sorted list of valid indices, or empty list on error.
    """
    if choice.lower() == "all":
        return list(range(1, max_items + 1))

    selected = set()
    for part in choice.split(","):
        part = part.strip()
        if "-" in part:
            try:
                start, end = part.split("-", 1)
                for i in range(int(start), int(end) + 1):
                    selected.add(i)
            except ValueError:
                print(f"Invalid range: {part}", file=sys.stderr)
                return []
        else:
            try:
                selected.add(int(part))
            except ValueError:
                print(f"Invalid number: {part}", file=sys.stderr)
                return []

    # Filter to valid range
    valid = sorted(i for i in selected if 1 <= i <= max_items)
    invalid = sorted(i for i in selected if i < 1 or i > max_items)
    for i in invalid:
        print(f"Warning: #{i} out of range, skipping.", file=sys.stderr)

    return valid


def _select_workspace() -> tuple[str, "Path", str | None] | None:
    """Show all Cursor workspaces and let the user pick one.

    Returns (project_path, workspace_dir, host) for the selected workspace, or None.
    """
    from .interactive import select_workspace as tui_select_workspace

    workspaces = paths.list_workspaces_with_conversations()
    if not workspaces:
        print("No Cursor workspaces found.")
        return None

    ws = tui_select_workspace(workspaces)
    if ws is None:
        return None
    return ws["path"], ws["workspace_dir"], ws.get("host")


def _select_conversations(project_path: str, prompt: str = "push", workspace_dir: "Path | None" = None) -> list[str]:
    """Show conversations for a workspace and let the user pick.

    Returns a list of selected composer IDs, or empty list.
    """
    from .interactive import select_conversations as tui_select_conversations

    conversations = export.list_conversations(project_path, workspace_dir=workspace_dir)
    if not conversations:
        print(f"No conversations found for {project_path}")
        return []

    conversations.sort(key=lambda c: c.get("lastUpdated", ""), reverse=True)

    project_name = os.path.basename(os.path.normpath(project_path)) or project_path
    print(f"\n  {project_name}: {len(conversations)} conversation(s)\n")

    return tui_select_conversations(conversations, action=prompt)


def _find_conversations_to_push() -> list[dict]:
    """Scan workspaces for conversations that need pushing (never_pushed or local_ahead)."""
    from .chat_lifecycle import is_excluded

    workspaces = paths.list_workspaces_with_conversations()
    push_items: list[dict] = []

    global_db_path = paths.get_global_db_path()
    if not global_db_path.exists():
        return push_items

    with db.CursorDB(global_db_path) as global_cdb:
        for ws in workspaces:
            ws_dir = ws["workspace_dir"]
            db_path = ws_dir / "state.vscdb"
            if not db_path.exists():
                continue

            composer_ids = paths.get_workspace_composer_ids(db_path)
            if not composer_ids:
                continue

            project_id = paths.get_project_identifier(ws["path"])

            for cid in composer_ids:
                if is_excluded(cid):
                    continue
                status = get_push_status_for_conversation(cid, project_id, _cdb=global_cdb)
                if status not in ("local_ahead", "never_pushed"):
                    continue
                cd = global_cdb.get_json(f"composerData:{cid}")
                name = cd.get("name", "Untitled") if cd else "Untitled"

                ws_name = os.path.basename(os.path.normpath(ws["path"])) or ws["path"]
                host = ws.get("host", "")
                ws_label = f"{ws_name} ({host})" if host else ws_name
                push_items.append({
                    "composerId": cid,
                    "name": name,
                    "workspace_label": ws_label,
                    "workspace_dir": ws_dir,
                    "project_path": ws["path"],
                    "host": host,
                    "push_status": status,
                })

    return push_items


def _export_and_push(sync_dir: Path, items: list[dict], backend: Optional[SyncBackend] = None) -> int:
    """Export a list of ahead conversation items and push via the backend.

    Returns the number of conversations successfully exported.
    """
    from collections import defaultdict

    by_workspace: dict[tuple, list[dict]] = defaultdict(list)
    for item in items:
        key = (item["project_path"], str(item["workspace_dir"]))
        by_workspace[key].append(item)

    total_saved = 0
    for (project_path, ws_dir_str), ws_items in by_workspace.items():
        ws_dir = Path(ws_dir_str)
        host = ws_items[0].get("host")
        composer_ids = [it["composerId"] for it in ws_items]
        saved = export.checkpoint_project(
            project_path,
            composer_ids=composer_ids,
            workspace_dir=ws_dir,
            source_host=host or None,
        )
        total_saved += len(saved)

    if total_saved == 0:
        return 0

    if backend is None:
        backend = get_backend()

    snapshots_dir = paths.get_snapshots_dir()
    if backend.has_remote():
        print("  Pushing...", end="", flush=True)
        if backend.push(snapshots_dir):
            print(" done")
        else:
            print(" failed", file=sys.stderr)

    return total_saved


def _push_ahead(sync_dir: Path, auto: bool = False, backend: Optional[SyncBackend] = None) -> int:
    """Find conversations ahead of snapshots and push them.

    Args:
        sync_dir: The sync repo directory.
        auto: If True, skip prompts and push all ahead conversations.
        backend: Sync backend to use for push. Auto-detected if None.

    Returns the number of conversations pushed.
    """
    if backend is None:
        backend = get_backend()

    if not auto:
        if backend.has_remote():
            snapshots_dir = paths.get_snapshots_dir()
            print("Syncing with remote...", end="", flush=True)
            if backend.pull(snapshots_dir):
                print(" done")
            else:
                print(" failed (continuing with local state)", file=sys.stderr)

    push_items = _find_conversations_to_push()

    if not push_items:
        if not auto:
            print("All synced conversations are up to date.")
        return 0

    if auto:
        never_count = sum(1 for i in push_items if i.get("push_status") == "never_pushed")
        ahead_count = len(push_items) - never_count
        if never_count and ahead_count:
            print(
                f"  Pushing {len(push_items)} conversation(s) "
                f"({never_count} new, {ahead_count} ahead)..."
            )
        elif never_count:
            print(f"  Pushing {never_count} new conversation(s)...")
        else:
            print(f"  Pushing {ahead_count} ahead conversation(s)...")
        for item in push_items:
            name = item["name"]
            if len(name) > 40:
                name = name[:37] + "..."
            tag = "new" if item.get("push_status") == "never_pushed" else "ahead"
            print(f"    {name} [{item['workspace_label']}] ({tag})")
        total = _export_and_push(sync_dir, push_items, backend=backend)
        return total

    print(f"\n  {len(push_items)} conversation(s) to push:\n")
    print(f"  {'#':<4} {'Name':<36} {'Workspace'}")
    print(f"  {'-' * 70}")

    for i, item in enumerate(push_items, 1):
        name = item["name"]
        if len(name) > 34:
            name = name[:31] + "..."
        ws_label = item["workspace_label"]
        if len(ws_label) > 30:
            ws_label = ws_label[:27] + "..."
        tag = "new" if item.get("push_status") == "never_pushed" else "ahead"
        print(f"  {i:<4} {name:<36} {ws_label} ({tag})")

    print(f"\n  Push these? (e.g. 1,3,5 or 1-3 or 'all') [all]:")
    try:
        choice = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return 0

    if not choice:
        choice = "all"

    indices = _parse_selection(choice, len(push_items))
    if not indices:
        print("No conversations selected.")
        return 0

    selected = [push_items[i - 1] for i in indices]
    total = _export_and_push(sync_dir, selected, backend=backend)

    if total == 0:
        print("No conversations exported.")
    else:
        print(f"\n  {total} conversation(s) checkpointed")

    print(f"\nDone. {total} conversation(s) pushed.")
    return total


def _get_sync_state_path() -> Path:
    """Path for local sync state (outside the git repo to survive git clean)."""
    return Path.home() / ".config" / "cursaves" / "sync_state.json"


def _load_sync_state() -> dict:
    """Load the local sync state (tracks diverged snapshots to avoid re-importing)."""
    state_path = _get_sync_state_path()
    if state_path.exists():
        try:
            return json.loads(state_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_sync_state(state: dict):
    """Persist the local sync state."""
    state_path = _get_sync_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2))


def _pull_behind(sync_dir: Path) -> int:
    """Find all snapshots where local is behind and import them automatically.

    For each behind/new snapshot, finds workspaces that already have the
    conversation registered and imports only into those.  This prevents
    duplicating imports across every matching workspace.

    Returns the number of snapshots successfully imported.
    """
    from .chat_lifecycle import is_excluded

    projects = list_snapshot_projects()
    if not projects:
        return 0

    global_db_path = paths.get_global_db_path()
    global_cdb = db.CursorDB(global_db_path) if global_db_path.exists() else None

    sync_state = _load_sync_state()
    handled = sync_state.get("handled_diverged", {})

    total_imported = 0
    backed_up_global = False
    backed_up_ws: set[str] = set()

    try:
        for project in projects:
            snapshot_files = list_snapshot_files(project["path"])
            if not snapshot_files:
                continue

            behind_snapshots: list[tuple[Path, dict]] = []
            for sf in snapshot_files:
                meta = read_snapshot_meta(sf)
                cid = meta.get("composerId")
                if not cid:
                    continue

                if is_excluded(cid):
                    continue

                # Skip snapshots we've already handled as diverged
                msg_count = meta.get("messageCount", 0)
                prev_handled = handled.get(cid)
                if prev_handled and prev_handled >= msg_count:
                    continue

                status = get_sync_status_for_snapshot(cid, msg_count, _cdb=global_cdb)
                if status in ("behind", "not_local"):
                    behind_snapshots.append((sf, meta))

            if not behind_snapshots:
                continue

            # Find all matching workspaces for this project
            all_matches = []
            seen_ws_dirs: set[str] = set()
            for sp in sorted(project.get("source_paths", set())):
                matches = paths.find_all_matching_workspaces(sp)
                for ws in matches:
                    ws_dir_str = str(ws["workspace_dir"])
                    if ws_dir_str not in seen_ws_dirs:
                        seen_ws_dirs.add(ws_dir_str)
                        all_matches.append(ws)

            if not all_matches:
                continue

            # Build a map: composerId -> list of workspaces that have it registered
            cid_to_workspaces: dict[str, list[dict]] = {}
            for ws in all_matches:
                ws_db_path = ws["workspace_dir"] / "state.vscdb"
                if not ws_db_path.exists():
                    continue
                ws_composer_ids = set(paths.get_workspace_composer_ids(ws_db_path))
                for sf, meta in behind_snapshots:
                    cid = meta.get("composerId", "")
                    if cid in ws_composer_ids:
                        cid_to_workspaces.setdefault(cid, []).append(ws)

            for sf, meta in behind_snapshots:
                cid = meta.get("composerId", "")
                target_list = cid_to_workspaces.get(cid, [])

                if not target_list:
                    # Not registered anywhere — pick the first matching workspace
                    target_list = all_matches[:1]

                for ws in target_list:
                    if not backed_up_global and global_db_path.exists():
                        db.backup_db(global_db_path)
                        backed_up_global = True

                    ws_dir_str = str(ws["workspace_dir"])
                    if ws_dir_str not in backed_up_ws:
                        ws_db_path = ws["workspace_dir"] / "state.vscdb"
                        if ws_db_path.exists():
                            db.backup_db(ws_db_path)
                        backed_up_ws.add(ws_dir_str)

                    ok = import_snapshot(
                        sf, ws["path"],
                        target_workspace_dir=ws["workspace_dir"],
                        skip_backup=True,
                    )
                    if ok:
                        total_imported += 1

                # Record that we've handled this snapshot at this message count
                # so diverged conversations don't get re-imported every sync
                msg_count = meta.get("messageCount", 0)
                handled[cid] = msg_count
    finally:
        if global_cdb:
            global_cdb.close()

    # Persist sync state so handled diverged snapshots are remembered
    sync_state["handled_diverged"] = handled
    _save_sync_state(sync_state)

    return total_imported


def cmd_repair(args):
    """Repair conversations with missing agent blobs by restoring from snapshots."""
    print("Scanning for missing blobs...")
    fixed, restored = repair_missing_blobs(verbose=True)
    if fixed > 0:
        print(f"\nRepaired {fixed} conversation(s), restored {restored} blob(s).")
        print("Restart Cursor to apply fixes.")
    elif restored == 0 and fixed == 0:
        print("\nNo blobs could be restored from available snapshots.")
        print("To fix remaining conversations, re-push them from the original machine")
        print("using the latest cursaves (which exports agent blobs).")


def _profile_sync_enabled(args=None) -> bool:
    if args is not None and getattr(args, "no_profile", False):
        return False
    return profile.is_profile_enabled()


def _ensure_profile_config() -> None:
    """Persist default profile settings when missing from config."""
    config = load_config()
    if "profile" not in config:
        config["profile"] = {
            "enabled": True,
            "categories": dict(profile.DEFAULT_CATEGORIES),
        }
        save_config(config)


def _save_git_identity_from_args(args) -> None:
    """Save git identity from CLI flags if provided."""
    git_name = getattr(args, "git_name", None)
    git_email = getattr(args, "git_email", None)
    sign_commits = getattr(args, "git_sign", None)

    if git_name is None and git_email is None and sign_commits is None:
        return

    current = get_git_config()
    save_git_config(
        name=git_name if git_name is not None else current.get("name"),
        email=git_email if git_email is not None else current.get("email"),
        sign_commits=sign_commits if sign_commits is not None else current.get("sign_commits", False),
    )


def cmd_config_git(args):
    """Configure git identity for sync repo commits."""
    if args.show:
        git_cfg = get_git_config()
        print("Git identity for sync commits (~/.cursaves/):")
        print(f"  Name:  {git_cfg.get('name') or '(not set)'}")
        print(f"  Email: {git_cfg.get('email') or '(not set)'}")
        print(f"  Sign commits (GPG): {'yes' if git_cfg.get('sign_commits') else 'no'}")
        global_id = read_global_git_identity()
        if global_id.get("name") or global_id.get("email"):
            print("\nGlobal git identity (for reference):")
            print(f"  Name:  {global_id.get('name') or '(not set)'}")
            print(f"  Email: {global_id.get('email') or '(not set)'}")
        return

    if not args.name and not args.email and args.sign is None and not args.no_sign:
        print(
            "Error: specify --name, --email, --sign, --no-sign, or --show.",
            file=sys.stderr,
        )
        sys.exit(1)

    current = get_git_config()
    if args.no_sign:
        sign_commits = False
    elif args.sign is not None:
        sign_commits = True
    else:
        sign_commits = current.get("sign_commits", False)
    git_cfg = save_git_config(
        name=args.name if args.name else current.get("name"),
        email=args.email if args.email else current.get("email"),
        sign_commits=sign_commits,
    )
    print("Git identity saved for sync commits:")
    print(f"  Name:  {git_cfg.get('name') or '(not set)'}")
    print(f"  Email: {git_cfg.get('email') or '(not set)'}")
    print(f"  Sign commits (GPG): {'yes' if git_cfg.get('sign_commits') else 'no'}")


def cmd_config_sync(args):
    """Configure sync lifecycle settings (retention, pins, exclusions)."""
    from .chat_lifecycle import get_sync_config, save_sync_config

    if args.show:
        sync = get_sync_config()
        print("Sync lifecycle settings (~/.config/cursaves/config.json):")
        print(f"  chat_enabled:          {sync.get('chat_enabled')}")
        print(f"  retention_days:        {sync.get('retention_days')} (0 = off)")
        print(f"  retention_purge_local: {sync.get('retention_purge_local')}")
        pinned = sync.get("pinned_composers") or []
        excluded = sync.get("excluded_composers") or []
        print(f"  pinned_composers:      {len(pinned)}")
        for cid in pinned[:10]:
            print(f"    {cid}")
        if len(pinned) > 10:
            print(f"    ... and {len(pinned) - 10} more")
        print(f"  excluded_composers:    {len(excluded)}")
        for cid in excluded[:10]:
            print(f"    {cid}")
        if len(excluded) > 10:
            print(f"    ... and {len(excluded) - 10} more")
        return

    updates = {}
    if args.retention_days is not None:
        updates["retention_days"] = args.retention_days
    if args.retention_purge_local is not None:
        updates["retention_purge_local"] = args.retention_purge_local
    if args.chat_enabled is not None:
        updates["chat_enabled"] = args.chat_enabled

    if not updates:
        print(
            "Error: specify --retention-days, --retention-purge-local, "
            "--chat-enabled, --no-chat-enabled, or --show.",
            file=sys.stderr,
        )
        sys.exit(1)

    sync = save_sync_config(updates)
    print("Sync settings saved:")
    print(f"  chat_enabled:          {sync.get('chat_enabled')}")
    print(f"  retention_days:        {sync.get('retention_days')}")
    print(f"  retention_purge_local: {sync.get('retention_purge_local')}")


def cmd_config(args):
    """Configuration subcommands."""
    if args.config_command == "git":
        cmd_config_git(args)
    elif args.config_command == "sync":
        cmd_config_sync(args)
    else:
        print("Error: unknown config command.", file=sys.stderr)
        sys.exit(1)


def cmd_remove(args):
    """Remove chats from sync repo and Cursor; never sync again."""
    from .chat_lifecycle import remove_chats

    composer_ids: list[str] = []
    if args.id:
        composer_ids.append(args.id)
    if getattr(args, "ids", None):
        composer_ids.extend(i.strip() for i in args.ids.split(",") if i.strip())

    if not composer_ids:
        print("Error: specify --id or --ids.", file=sys.stderr)
        sys.exit(1)

    composer_ids = list(dict.fromkeys(composer_ids))

    if not args.yes:
        print(f"Remove {len(composer_ids)} chat(s) from sync and Cursor?")
        for cid in composer_ids:
            print(f"  {cid}")
        try:
            confirm = input("\nContinue? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if confirm not in ("y", "yes"):
            print("Cancelled.")
            return

    result = remove_chats(
        composer_ids,
        purge_local=not args.snapshot_only,
        push_remote=True,
        force=args.force,
    )
    print(
        f"Removed {result.removed_snapshots} snapshot(s), "
        f"purged {result.purged_local} local chat(s), "
        f"excluded {result.excluded} from future sync."
    )


def cmd_pin(args):
    """Pin or unpin chats (pinned chats skip retention pruning)."""
    from .chat_lifecycle import pin_composer, unpin_composer

    if not args.id:
        print("Error: --id is required.", file=sys.stderr)
        sys.exit(1)

    if args.unpin:
        unpin_composer(args.id)
        print(f"Unpinned {args.id}")
    else:
        pin_composer(args.id)
        print(f"Pinned {args.id} (exempt from retention pruning)")


def _configure_git_verbose(args) -> None:
    set_git_verbose(bool(getattr(args, "verbose", False)))


def _profile_export_push(backend: SyncBackend, snapshots_dir: Path) -> bool:
    """Export local Cursor profile and push before remote pull."""
    _configure_stdio()
    print("\n── Profile ──")
    had_local = profile.profile_has_local_changes()
    exported = profile.export_profile()
    if had_local:
        print(f"  Exported {exported} profile item(s) from local Cursor config")
        if backend.has_remote():
            print("  Pushing profile...", end="", flush=True)
            ok = backend.push_profile(snapshots_dir)
            if ok:
                print(" done", flush=True)
            else:
                print(" failed", file=sys.stderr)
                print("  See git errors above.", file=sys.stderr)
            return ok
        print("  No remote configured, profile saved locally only")
    elif exported:
        print("  Profile mirror up to date")
    else:
        print("  No profile files to export")
    return True


def _profile_apply_after_pull() -> int:
    """Apply staged profile files to local Cursor paths."""
    applied = profile.apply_profile()
    if applied:
        print(f"  Applied {applied} profile item(s) from remote")
    else:
        print("  Profile already up to date")
    return applied


def cmd_profile_push(args):
    """Export local Cursor profile and push to remote."""
    _configure_git_verbose(args)
    _require_sync_repo()
    _ensure_profile_config()
    backend = get_backend()
    snapshots_dir = paths.get_snapshots_dir()

    exported = profile.export_profile()
    print(f"Exported {exported} profile item(s)")

    if backend.has_remote():
        print("Pushing profile...", end="", flush=True)
        if backend.push_profile(snapshots_dir):
            print(" done")
        else:
            print(" failed", file=sys.stderr)
            print("  See git errors above.", file=sys.stderr)
            sys.exit(1)
    else:
        print("No remote configured — profile saved to ~/.cursaves/profile/")


def cmd_profile_pull(args):
    """Pull remote profile and apply to local Cursor paths."""
    _configure_git_verbose(args)
    _require_sync_repo()
    _ensure_profile_config()
    backend = get_backend()
    snapshots_dir = paths.get_snapshots_dir()

    if backend.has_remote():
        print("Syncing profile from remote...", end="", flush=True)
        if backend.pull(snapshots_dir):
            print(" done")
        else:
            print(" failed", file=sys.stderr)
            print("  See git errors above.", file=sys.stderr)
            sys.exit(1)
    else:
        print("No remote configured, applying local profile mirror only.")

    applied = profile.apply_profile()
    print(f"Applied {applied} profile item(s)")
    if applied:
        print("Restart Cursor to pick up settings, hooks, or skills changes.")


def cmd_profile_status(args):
    """Show profile sync status."""
    _ensure_profile_config()
    rows = profile.profile_status()
    print(profile.format_profile_status(rows))
    pending = [r for r in rows if r["state"] in ("local_only", "differ")]
    if pending:
        print(f"\n{len(pending)} item(s) differ from the profile mirror.")
        print("Run 'cursaves profile push' to upload or 'cursaves profile pull' to apply remote.")
    elif any(r["state"] != "missing" for r in rows):
        print("\nProfile mirror matches local Cursor config.")
    behind = [r for r in rows if r["state"] == "local_behind"]
    if behind:
        print(f"\n{len(behind)} item(s) are behind the repo (local deletions are not synced).")
        print("Run 'cursaves profile pull' to restore from remote.")


def cmd_skills(args):
    """List or delete synced skills."""
    from . import skills_hooks

    _require_sync_repo()
    if args.skills_command == "list":
        rows = skills_hooks.list_skills()
        print(skills_hooks.format_skills_list(rows))
        return
    if args.skills_command == "delete":
        if not args.name:
            print("Error: specify skill name to delete.", file=sys.stderr)
            sys.exit(1)
        if not getattr(args, "yes", False):
            print(f"Delete skill '{args.name}' from local and sync repo? [y/N] ", end="")
            if input().strip().lower() not in ("y", "yes"):
                print("Cancelled.")
                return
        if skills_hooks.delete_skill(args.name):
            print(f"Deleted skill '{args.name}' and pushed to remote.")
        else:
            print(f"Skill '{args.name}' not found.", file=sys.stderr)
            sys.exit(1)


def cmd_hooks(args):
    """List or delete synced hooks."""
    from . import skills_hooks

    _require_sync_repo()
    if args.hooks_command == "list":
        rows = skills_hooks.list_hooks()
        print(skills_hooks.format_hooks_list(rows))
        return
    if args.hooks_command == "delete":
        if not args.name:
            print("Error: specify hook name or command to delete.", file=sys.stderr)
            sys.exit(1)
        if not getattr(args, "yes", False):
            print(f"Delete hook '{args.name}' from local and sync repo? [y/N] ", end="")
            if input().strip().lower() not in ("y", "yes"):
                print("Cancelled.")
                return
        if skills_hooks.delete_hook(args.name):
            print(f"Deleted hook '{args.name}' and pushed to remote.")
        else:
            print(f"Hook '{args.name}' not found.", file=sys.stderr)
            sys.exit(1)


def cmd_setup(args):
    """Interactive first-time setup."""
    setup_wizard.run_setup(args)


def _apply_retention_prune(verbose: bool = False) -> int:
    """Prune expired snapshots and push deletions to remote."""
    from .chat_lifecycle import apply_retention

    result = apply_retention()
    if result.pruned <= 0:
        return 0

    sync_dir = paths.get_sync_dir()
    hostname = paths.get_machine_id()
    msg = f"[{hostname}] retention: pruned {result.pruned} snapshot(s)"
    if _commit_and_push(sync_dir, msg):
        if verbose:
            print(f"  Retention: pruned {result.pruned} snapshot(s)")
    return result.pruned


def cmd_sync(args):
    """Pull behind conversations then push ahead ones — fully automatic."""
    from .chat_lifecycle import is_chat_sync_enabled

    _configure_stdio()
    _configure_git_verbose(args)
    sync_dir = _require_sync_repo()
    _ensure_profile_config()
    backend = get_backend()
    snapshots_dir = paths.get_snapshots_dir()
    profile_applied = 0
    chat_sync = is_chat_sync_enabled()
    imported = 0
    pushed = 0

    if _profile_sync_enabled(args):
        if not _profile_export_push(backend, snapshots_dir):
            return

    # Step 1: Pull remote → local snapshots + profile
    if backend.has_remote():
        print("\nSyncing with remote...", end="", flush=True)
        if backend.pull(snapshots_dir):
            print(" done")
        else:
            print(" failed", file=sys.stderr)
            print("  Hint: run with --verbose or set CURSAVES_DEBUG=1 for git command details.", file=sys.stderr)
            return

    if _profile_sync_enabled(args):
        print("\n── Profile apply ──")
        profile_applied = _profile_apply_after_pull()

    if chat_sync:
        # Step 2: Import — pull behind conversations from snapshots into Cursor DBs
        print("\n── Pull ──")
        imported = _pull_behind(sync_dir)
        if imported > 0:
            print(f"  Imported {imported} conversation(s)")
        else:
            print("  Everything up to date")

        # Step 3: Retention — prune old snapshots before push
        pruned = _apply_retention_prune(verbose=getattr(args, "verbose", False))
        if pruned > 0 and not getattr(args, "verbose", False):
            print(f"  Retention: pruned {pruned} expired snapshot(s)")

        # Step 4: Push — export conversations from Cursor DBs into snapshots
        print("\n── Push ──")
        pushed = _push_ahead(sync_dir, auto=True, backend=backend)
        if pushed == 0:
            print("  Nothing to push")
    else:
        print("\n── Chat sync disabled (sync.chat_enabled=false) ──")

    # Summary
    print()
    if imported > 0 or pushed > 0 or profile_applied > 0:
        parts = []
        if profile_applied > 0:
            parts.append(f"{profile_applied} profile applied")
        if imported > 0:
            parts.append(f"{imported} pulled")
        if pushed > 0:
            parts.append(f"{pushed} pushed")
        print(f"Sync complete: {', '.join(parts)}.")
        if imported > 0:
            print("Restart Cursor to see imported chats.")
        elif profile_applied > 0:
            print("Restart Cursor to pick up profile changes.")
    else:
        print("Already in sync.")


def cmd_push(args):
    """Checkpoint + push in one command."""
    _configure_git_verbose(args)
    sync_dir = _require_sync_repo()
    backend = get_backend()
    snapshots_dir = paths.get_snapshots_dir()

    if getattr(args, "ahead", False):
        _push_ahead(sync_dir, backend=backend)
        return

    # Step 0: Pull latest from remote
    if backend.has_remote():
        if not backend.pull(snapshots_dir):
            print("Warning: Could not sync with remote, continuing anyway...", file=sys.stderr)

    # Resolve workspace and select conversations
    composer_ids = None
    workspace_dir = None
    source_host = None
    if args.select:
        result = _select_workspace()
        if not result:
            return
        project_path, workspace_dir, source_host = result
    else:
        project_path, workspace_dir, source_host = _resolve_project_and_workspace(args)

    # Always show conversation list for selection (unless --all flag)
    if not getattr(args, "all_chats", False):
        composer_ids = _select_conversations(project_path, prompt="push", workspace_dir=workspace_dir)
        if not composer_ids:
            print("No conversations selected.")
            return

    # Step 1: Checkpoint
    if composer_ids:
        print(f"\nCheckpointing {len(composer_ids)} conversation(s)...")
    else:
        print(f"Checkpointing all conversations for {project_path}...")
    saved = export.checkpoint_project(
        project_path, composer_ids=composer_ids,
        workspace_dir=workspace_dir, source_host=source_host,
    )

    if not saved:
        print("No conversations found to checkpoint.")
        return

    print(f"  {len(saved)} conversation(s) checkpointed")

    # Step 2: Push to remote
    if backend.has_remote():
        print("  Pushing...", end="", flush=True)
        if backend.push(snapshots_dir):
            print(" done")
        else:
            print(" failed", file=sys.stderr)
    else:
        print("  No remote configured, skipping push")

    print(f"\nDone. {len(saved)} conversation(s) saved.")


def _git_pull_quiet(sync_dir: Path) -> bool:
    """Pull from remote without printing status. Returns True on success."""
    backend = get_backend()
    snapshots_dir = paths.get_snapshots_dir()
    return backend.pull(snapshots_dir)


def _commit_and_push(sync_dir: Path, message: str) -> bool:
    """Push snapshot changes to the remote backend. Returns True on success."""
    backend = get_backend()
    snapshots_dir = paths.get_snapshots_dir()
    if backend.has_remote():
        return backend.push(snapshots_dir)
    return True


def _backend_pull() -> bool:
    """Pull latest snapshots from the configured backend."""
    backend = get_backend()
    snapshots_dir = paths.get_snapshots_dir()

    if not backend.has_remote():
        print("No remote configured, importing from local snapshots only.")
        return True

    print("Syncing with remote...", end="", flush=True)
    if backend.pull(snapshots_dir):
        print(" done")
        return True
    print(" failed", file=sys.stderr)
    print("  Hint: run with --verbose or set CURSAVES_DEBUG=1 for git command details.", file=sys.stderr)
    return False


def cmd_pull(args):
    """Pull + import snapshots in one command."""
    _configure_git_verbose(args)
    sync_dir = _require_sync_repo()

    # Step 1: Pull from remote
    if not _backend_pull():
        return

    # Step 2: Select what to import
    if args.select:
        from .interactive import select_one, select_snapshots

        # Interactive: show available snapshot projects and let user pick
        projects = list_snapshot_projects()
        if not projects:
            print("No snapshots found. Run 'cursaves push' on another machine first.")
            return

        # Select project with fuzzy search
        project_choices = []
        for p in projects:
            sources = ", ".join(sorted(p["sources"])) or "unknown"
            last_saved = p.get("latest_export", "")[:16] or "unknown"
            display = f"{p['name']:<30} {p['count']:>3} chats  {last_saved}  from {sources}"
            project_choices.append({"name": display, "_project": p})

        selected_project = select_one(
            project_choices, message="Select project to import from:"
        )
        if not selected_project:
            return

        total_success = 0
        total_failure = 0
        for project in [selected_project["_project"]]:
            # Build snapshot list for this project
            snapshot_files = list_snapshot_files(project["path"])
            snapshots_info = []
            for sf in snapshot_files:
                meta = read_snapshot_meta(sf)
                source_host = meta.get("sourceHost")
                snapshots_info.append({
                    "file": sf,
                    "composerId": meta.get("composerId"),
                    "name": meta.get("name") or "Untitled",
                    "msgs": meta.get("messageCount", 0),
                    "exported": (meta.get("exportedAt") or "")[:16] or "unknown",
                    "source": source_host or meta.get("sourceMachine") or "unknown",
                })

            if not snapshots_info:
                print(f"  No snapshots in {project['name']}/")
                continue

            # Interactive snapshot selection
            selected_snaps = select_snapshots(snapshots_info)
            if not selected_snaps:
                continue

            selected_files = [s["file"] for s in selected_snaps]
            print(f"\n  Importing {len(selected_files)} chat(s) from {project['name']}/...")

            # Find target workspace
            target_workspaces = _select_target_workspaces(project["source_paths"])

            if not target_workspaces:
                cwd = os.getcwd()
                cwd_basename = os.path.basename(os.path.normpath(cwd))
                source_basenames = {os.path.basename(os.path.normpath(sp)) for sp in project["source_paths"]}
                if cwd_basename in source_basenames or project["name"] == paths.get_project_identifier(cwd):
                    target_path = cwd
                else:
                    print(f"  No matching workspaces found.")
                    print(f"  Enter a local project path to import into (or press Enter to skip):")
                    try:
                        target_path = input("  > ").strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        continue
                    if not target_path:
                        print("  Skipped.")
                        continue

                for sf in selected_files:
                    print(f"  Importing {sf.name}...")
                    if import_snapshot(sf, target_path):
                        total_success += 1
                        print(f"    OK")
                    else:
                        total_failure += 1
                        print(f"    FAILED")
            else:
                for ws in target_workspaces:
                    display = paths.format_workspace_display(ws)
                    print(f"  Importing into: {display}")
                    for sf in selected_files:
                        print(f"    {sf.name}...")
                        if import_snapshot(sf, ws["path"], target_workspace_dir=ws["workspace_dir"]):
                            total_success += 1
                        else:
                            total_failure += 1

        if total_success == 0 and total_failure == 0:
            print("\nNo snapshots imported.")
            return

        print(f"\nDone: {total_success} imported, {total_failure} failed.")
        if total_success > 0:
            _maybe_reload(args)
    else:
        # Non-interactive: import for the resolved project/workspace
        project_path, workspace_dir = _resolve_workspace_for_import(args)
        if workspace_dir:
            # Show which workspace we're importing into
            ws_info = paths.format_workspace_display(
                {"type": "ssh" if "ssh" in str(workspace_dir) else "local",
                 "host": None, "path": project_path},
                include_path=True
            )
            print(f"Importing into workspace: {project_path}")
        else:
            print(f"Importing snapshots for {project_path}...")

        success, failure = import_all_snapshots(
            project_path,
            force=args.force,
            target_workspace_dir=workspace_dir,
        )

        if success == 0 and failure == 0:
            print("No snapshots found to import.")
            return

        print(f"\nDone: {success} imported, {failure} failed.")
        if success > 0:
            _maybe_reload(args)


def cmd_watch(args):
    """Run the background watch daemon."""
    if getattr(args, "uninstall_hook", False):
        uninstall_watch_hook()
        return

    if getattr(args, "install_hook", False):
        interval = args.interval if args.interval != 60 else 120
        install_watch_hook(interval=interval)
        return

    if not getattr(args, "watch_all", False):
        print(
            "Error: use 'cursaves watch --all' to run sync, or "
            "'cursaves watch --install-hook' for automatic sync when Cursor opens.",
            file=sys.stderr,
        )
        sys.exit(1)

    interval = args.interval if args.interval != 60 else 120
    poll_interval = getattr(args, "poll_interval", POLL_INTERVAL_DEFAULT)

    if getattr(args, "detach", False):
        started = detach_watch_all(
            interval=interval,
            git_sync=not args.no_git,
            verbose=args.verbose,
            poll_interval=poll_interval,
        )
        if not started and args.verbose:
            print("watch --all already running.")
        return

    watch_all_loop(
        interval=interval,
        git_sync=not args.no_git,
        verbose=args.verbose,
        poll_interval=poll_interval,
    )


def cmd_copy(args):
    """Copy conversations between workspaces on the same machine."""
    # Select source workspace
    print(f"\n  Select SOURCE workspace (copy from):")
    source = _select_workspace()
    if not source:
        return
    source_path, source_ws_dir, source_host = source

    # Select conversations from source
    composer_ids = _select_conversations(
        source_path, prompt="copy", workspace_dir=source_ws_dir
    )
    if not composer_ids:
        print("No conversations selected.")
        return

    # Select target workspace
    print(f"\n  Select TARGET workspace (copy to):")
    target = _select_workspace()
    if not target:
        return
    target_path, target_ws_dir, target_host = target

    if str(source_ws_dir) == str(target_ws_dir):
        print("Source and target are the same workspace.", file=sys.stderr)
        return

    source_label = f"{os.path.basename(source_path)}"
    target_label = f"{os.path.basename(target_path)}"
    if source_host:
        source_label += f" ({source_host})"
    if target_host:
        target_label += f" ({target_host})"

    print(f"\n  Copying {len(composer_ids)} chat(s): {source_label} → {target_label}\n")

    success, failure = copy_between_workspaces(
        composer_ids, source_ws_dir, target_ws_dir,
        source_path=source_path, target_path=target_path,
        force=getattr(args, "force", False),
    )

    if success > 0:
        print(f"\nDone. Copied {success} chat(s).")
        from .reload import print_reload_hint
        print_reload_hint()
    elif failure > 0:
        print(f"\nFailed to copy {failure} chat(s).")
    else:
        print("Nothing done.")


def cmd_status(args):
    """Show sync status -- what's local vs what's in snapshots."""
    _ensure_synced()  # Pull latest from remote first
    project_path, workspace_dir, _ = _resolve_project_and_workspace(args)
    project_id = paths.get_project_identifier(project_path)
    snapshots_dir = paths.get_snapshots_dir() / project_id

    # Get local conversations
    local_convos = export.list_conversations(project_path, workspace_dir=workspace_dir)
    local_ids = {c["id"] for c in local_convos}

    # Get snapshot conversations
    snapshot_ids = set()
    if snapshots_dir.exists():
        for f in list_snapshot_files(snapshots_dir):
            snapshot_ids.add(_get_snapshot_id(f))

    only_local = local_ids - snapshot_ids
    only_snapshot = snapshot_ids - local_ids
    in_both = local_ids & snapshot_ids

    print(f"Project: {project_path}")
    print(f"Identity: {project_id}")
    print(f"Snapshots: {snapshots_dir}\n")
    print(f"  Local conversations:     {len(local_ids)}")
    print(f"  Snapshot files:          {len(snapshot_ids)}")
    print(f"  In both:                 {len(in_both)}")
    print(f"  Local only (unexported): {len(only_local)}")
    print(f"  Snapshot only (not imported): {len(only_snapshot)}")

    if only_local:
        print(f"\nLocal only (run 'checkpoint' to export):")
        for c in local_convos:
            if c["id"] in only_local:
                print(f"  {c['id'][:12]}...  {c['name']}")

    if only_snapshot:
        print(f"\nSnapshot only (run 'import --all' to import):")
        for sid in sorted(only_snapshot):
            print(f"  {sid[:12]}...")


def cmd_delete(args):
    """Delete cached snapshots and sync to remote."""
    import shutil

    sync_dir = paths.get_sync_dir()
    snapshots_base = paths.get_snapshots_dir()
    backend = get_backend()

    if backend.has_remote():
        backend.pull(snapshots_base)

    deleted_any = False

    # --all-projects: delete everything
    if args.all_projects:
        projects = list_snapshot_projects(snapshots_base)
        if not projects:
            print("No snapshots found.")
            return

        total_count = sum(p["count"] for p in projects)
        if not args.yes:
            print(f"This will delete {total_count} snapshot(s) across {len(projects)} project(s):")
            for p in projects:
                print(f"  {p['name']}: {p['count']} snapshot(s)")
            try:
                confirm = input("\nContinue? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if confirm not in ("y", "yes"):
                print("Cancelled.")
                return

        for p in projects:
            shutil.rmtree(p["path"])
            print(f"  Deleted: {p['name']}/ ({p['count']} snapshots)")

        print(f"\nDeleted {total_count} snapshot(s) across {len(projects)} project(s).")

        # Sync deletion to remote
        hostname = paths.get_machine_id()
        if _commit_and_push(sync_dir, f"[{hostname}] delete all snapshots"):
            print("Synced to remote.")
        return

    # --select: interactive selection across projects
    if args.select:
        from .interactive import select_many, confirm as tui_confirm

        projects = list_snapshot_projects(snapshots_base)
        if not projects:
            print("No snapshots found.")
            return

        project_choices = []
        for p in projects:
            sources = ", ".join(sorted(p["sources"])) or "unknown"
            display = f"{p['name']:<40} {p['count']:>3} chats  from {sources}"
            project_choices.append({"name": display, "_project": p})

        selected = select_many(
            project_choices,
            message="Select projects to delete (space=toggle, type to filter):",
            name_key="name",
        )
        if not selected:
            return

        selected_projects = [s["_project"] for s in selected]
        total_count = sum(p["count"] for p in selected_projects)

        if not tui_confirm(f"Delete {total_count} snapshot(s) across {len(selected_projects)} project(s)?"):
            print("Cancelled.")
            return

        total_deleted = 0
        deleted_names = []
        for p in selected_projects:
            shutil.rmtree(p["path"])
            print(f"  Deleted: {p['name']}/ ({p['count']} snapshots)")
            total_deleted += p["count"]
            deleted_names.append(p["name"])

        print(f"\nDeleted {total_deleted} snapshot(s) across {len(indices)} project(s).")

        # Sync deletion to remote
        hostname = paths.get_machine_id()
        msg = f"[{hostname}] delete {', '.join(deleted_names[:3])}"
        if len(deleted_names) > 3:
            msg += f" +{len(deleted_names) - 3} more"
        if _commit_and_push(sync_dir, msg):
            print("Synced to remote.")
        return

    # Single project mode (original behavior)
    project_path = args.project or paths.get_project_path()
    project_id = paths.get_project_identifier(project_path)
    snapshots_dir = snapshots_base / project_id

    if not snapshots_dir.exists():
        print(f"No snapshots found for {project_path}")
        return

    snapshot_files = list_snapshot_files(snapshots_dir)
    if not snapshot_files:
        print(f"No snapshots found for {project_path}")
        return

    if args.all:
        # Delete all snapshots for this project
        count = len(snapshot_files)
        if not args.yes:
            print(f"This will delete {count} snapshot(s) from {snapshots_dir}")
            try:
                confirm = input("Continue? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if confirm not in ("y", "yes"):
                print("Cancelled.")
                return

        for f in snapshot_files:
            _delete_snapshot(f)
        print(f"Deleted {count} snapshot(s).")

        # Sync deletion to remote
        hostname = paths.get_machine_id()
        if _commit_and_push(sync_dir, f"[{hostname}] delete all from {project_id}"):
            print("Synced to remote.")
        return

    if args.id:
        # Delete a specific snapshot by ID (supports partial match)
        target = args.id
        matches = [f for f in snapshot_files if _get_snapshot_id(f).startswith(target)]
        if not matches:
            print(f"No snapshot matching '{target}' found.", file=sys.stderr)
            sys.exit(1)
        if len(matches) > 1:
            print(f"Multiple snapshots match '{target}':", file=sys.stderr)
            for f in matches:
                print(f"  {_get_snapshot_id(f)}", file=sys.stderr)
            print("Be more specific.", file=sys.stderr)
            sys.exit(1)

        match = matches[0]
        _delete_snapshot(match)
        print(f"Deleted {_get_snapshot_id(match)}")

        # Sync deletion to remote
        hostname = paths.get_machine_id()
        if _commit_and_push(sync_dir, f"[{hostname}] delete {_get_snapshot_id(match)[:12]}"):
            print("Synced to remote.")
        return

    # Interactive mode: list and select snapshots for current project
    print(f"\nCached snapshots for {project_path}\n")
    snapshot_info = []
    for i, f in enumerate(snapshot_files, 1):
        meta = read_snapshot_meta(f)
        name = meta.get("name") or "Untitled"
        exported_at = meta.get("exportedAt") or "unknown"
        source = meta.get("sourceMachine") or "unknown"

        if len(name) > 33:
            name = name[:30] + "..."
        snapshot_info.append({"file": f, "name": name, "exported_at": exported_at, "source": source})
        print(f"  {i:<4} {name:<35} {exported_at[:19]:<20} from {source}")

    print(f"\nEnter numbers to delete (e.g. 1,3,5 or 1-3 or 'all'):")
    try:
        choice = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if not choice:
        return

    indices = _parse_selection(choice, len(snapshot_info))
    if not indices:
        return

    deleted_names = []
    for idx in indices:
        _delete_snapshot(snapshot_info[idx - 1]["file"])
        print(f"  Deleted: {snapshot_info[idx - 1]['name']}")
        deleted_names.append(snapshot_info[idx - 1]["name"])

    print(f"\nDeleted {len(indices)} snapshot(s).")

    # Sync deletion to remote
    hostname = paths.get_machine_id()
    if _commit_and_push(sync_dir, f"[{hostname}] delete {len(indices)} from {project_id}"):
        print("Synced to remote.")


def cmd_doctor(args):
    """Audit and recover orphaned chats."""
    from .export import format_timestamp

    audit = doctor_audit()
    storage = audit["storage"]

    print(
        f"\n  ─── Cursor Storage ──────────────────────────────────────────\n"
        f"\n"
        f"  Global DB:           {storage['global_db_mb']:.0f} MB\n"
        f"  WAL file:            {storage.get('wal_mb', 0):.1f} MB\n"
        f"  Workspace storage:   {storage['workspace_storage_mb']:.0f} MB\n"
    )

    print(
        f"  ─── Chat Audit ─────────────────────────────────────────────\n"
        f"\n"
        f"  Total chats in DB:   {audit['total']}\n"
        f"  Registered:          {audit['registered']}\n"
        f"  Orphaned (content):  {len(audit['orphaned'])}\n"
        f"  Empty stubs:         {audit['empty']}\n"
    )

    if audit["workspaces"]:
        print(
            f"  ─── Workspaces with chats ───────────────────────────────────\n"
        )
        for ws in audit["workspaces"]:
            print(f"  {ws['chat_count']:>3} chats   {ws['label']}")
        print()

    orphaned = audit["orphaned"]
    if not orphaned:
        print("  No orphaned chats found.\n")
        return

    print(
        f"  ─── Orphaned chats ({len(orphaned)}) ──────────────────────────────────\n"
    )
    print(f"  {'#':<4} {'Name':<36} {'Msgs':>5}  {'Likely Workspace'}")
    print(f"  {'-' * 75}")

    for i, chat in enumerate(orphaned, 1):
        name = chat["name"]
        if len(name) > 34:
            name = name[:31] + "..."
        ws = chat.get("likelyWorkspace") or "unknown"
        if len(ws) > 22:
            ws = ws[:19] + "..."
        print(f"  {i:<4} {name:<36} {chat['messageCount']:>5}  {ws}")

    print()

    if args.recover:
        if args.select:
            print(f"  Select chats to recover (e.g. 1,3,5 or 1-3 or 'all') [all]:")
            try:
                choice = input("  > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not choice:
                choice = "all"
            indices = _parse_selection(choice, len(orphaned))
            if not indices:
                return
            selected_ids = [orphaned[i - 1]["composerId"] for i in indices]
        else:
            selected_ids = [o["composerId"] for o in orphaned]

        print(f"\n  Recovering {len(selected_ids)} chat(s)...\n")
        recovered, failed = doctor_recover(composer_ids=selected_ids, force=getattr(args, "force", False))

        if recovered > 0:
            print(f"\n  Recovered {recovered} chat(s).")
            from .reload import print_reload_hint
            print_reload_hint()
        if failed > 0:
            print(f"  {failed} chat(s) could not be matched to a workspace.")
    else:
        print(
            f"  Run 'cursaves doctor --recover' to re-register orphaned chats.\n"
            f"  Run 'cursaves doctor --recover -s' to select which chats to recover.\n"
        )


def cmd_purge(args):
    """Delete chats from Cursor's database to reclaim space."""
    from .importer import list_all_chats_with_sizes, purge_chats
    from .interactive import select_purge_chats, confirm as tui_confirm

    force = getattr(args, "force", False)
    ws_filter = getattr(args, "workspace", None)

    print("\n  Scanning chats (this may take a moment)...\n")
    all_chats = list_all_chats_with_sizes()

    if not all_chats:
        print("  No chats found.")
        return

    if ws_filter:
        ws_filter_lower = ws_filter.lower()
        all_chats = [
            c for c in all_chats
            if ws_filter_lower in c["workspace_label"].lower()
        ]
        if not all_chats:
            print(f"  No chats matching workspace '{ws_filter}'.")
            return

    with_content = [c for c in all_chats if c["messageCount"] > 0 or c["name"]]
    stubs = [c for c in all_chats if c["messageCount"] == 0 and not c["name"]]
    total_keys = sum(c["keyCount"] for c in all_chats)

    print(
        f"  Found {len(all_chats)} chats ({total_keys:,} DB keys)\n"
        f"  {len(with_content)} with content, {len(stubs)} empty stubs\n"
    )

    # Use interactive TUI for selection
    selected_ids = select_purge_chats(with_content + stubs)

    if not selected_ids:
        print("  Nothing selected.")
        return

    selected_keys = sum(
        c["keyCount"] for c in all_chats if c["composerId"] in set(selected_ids)
    )
    print(
        f"\n  Will delete {len(selected_ids)} chat(s) "
        f"({selected_keys:,} DB keys)."
    )

    if not tui_confirm("Continue with deletion?"):
        print("  Cancelled.")
        return

    deleted, keys_removed = purge_chats(selected_ids, force=force)
    print(f"\n  Deleted {deleted} chat(s), removed {keys_removed:,} DB keys.")
    print("  Run VACUUM on the global DB to reclaim disk space:")
    print(f"    sqlite3 '{paths.get_global_db_path()}' 'VACUUM;'")
    print()


def _resolve_move_targets(id_query: Optional[str], from_ws: Optional[str]) -> tuple[list[dict], "Optional[str]"]:
    """Resolve the set of chats to move from --id / --from selectors.

    Returns (chats, error) where chats is a list of {id, name, ws} dicts.
    On error, chats is [] and error is a message string.
    """
    headers_map = paths._build_global_headers_map()
    # Flat index: composerId -> (name, current_ws_id)
    index: dict[str, tuple[str, str]] = {}
    for ws_id, entries in headers_map.items():
        for e in entries:
            cid = e.get("composerId")
            if cid:
                index[cid] = (e.get("name", ""), ws_id)

    chosen: dict[str, dict] = {}

    if from_ws:
        src = paths.resolve_workspace(from_ws)
        if src is None:
            return [], f"No workspace matching '{from_ws}'."
        src_hash = src["workspace_dir"].name
        for cid, (name, ws_id) in index.items():
            if ws_id == src_hash:
                chosen[cid] = {"id": cid, "name": name, "ws": ws_id}
        if not chosen:
            return [], f"No chats tagged to workspace '{from_ws}' ({src_hash[:12]})."

    if id_query:
        matches = {
            cid: (name, ws_id)
            for cid, (name, ws_id) in index.items()
            if cid == id_query or cid.startswith(id_query) or id_query in cid
        }
        if not matches:
            return [], f"No chat matching id '{id_query}'."
        for cid, (name, ws_id) in matches.items():
            chosen[cid] = {"id": cid, "name": name, "ws": ws_id}

    return list(chosen.values()), None


def cmd_move_chat(args):
    """Re-tag chats to a different workspace (change their workspaceIdentifier)."""
    from .importer import move_chat

    if not args.id and not args.from_ws:
        print("Error: specify --id <composer-id> and/or --from <workspace>.", file=sys.stderr)
        sys.exit(1)

    to_ws = paths.resolve_workspace(args.to)
    if to_ws is None:
        print(
            f"Error: No workspace matching '{args.to}'.\n"
            f"Run 'cursaves workspaces' to see available workspaces "
            f"(or use 'unassigned' to strip the workspace tag).",
            file=sys.stderr,
        )
        sys.exit(1)
    to_hash = to_ws["workspace_dir"].name

    chats, err = _resolve_move_targets(args.id, args.from_ws)
    if err:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)

    # Drop chats already tagged to the destination.
    chats = [c for c in chats if c["ws"] != to_hash]
    if not chats:
        print("Nothing to move (chats already in the destination workspace).")
        return

    dest_label = to_hash[:12] if to_hash == paths.UNASSIGNED_WS_ID else f"{to_ws['path']} ({to_hash[:12]})"
    print(f"\nMove {len(chats)} chat(s) -> {dest_label}\n")
    for c in chats:
        name = c["name"] or "(untitled)"
        if len(name) > 44:
            name = name[:41] + "..."
        print(f"  {c['id'][:8]}  from {c['ws'][:12]:<13} {name}")

    if args.dry_run:
        print("\nDry run: no changes written.")
        return

    if not args.yes:
        try:
            resp = input(f"\nProceed? [y/N] ").strip().lower()
        except EOFError:
            resp = ""
        if resp not in ("y", "yes"):
            print("Cancelled.")
            return

    moved, skipped = move_chat([c["id"] for c in chats], to_hash, force=args.force)
    if moved == 0 and skipped == 0:
        return  # move_chat already printed the reason (e.g. Cursor running)

    print(f"\nMoved {moved} chat(s)" + (f", skipped {skipped}" if skipped else "") + ".")
    if moved:
        paths.invalidate_headers_cache()
        from .reload import print_reload_hint
        print_reload_hint()


def cmd_migrate(args):
    """Migrate old chats to the Cursor 3.0 global index."""
    from .importer import migrate_to_global_headers

    dry_run = getattr(args, "dry_run", False)
    force = getattr(args, "force", False)

    if dry_run:
        print("\n  ─── Dry run: previewing migration ─────────────────────────\n")
    else:
        print("\n  ─── Migrating chats to Cursor 3.0 global index ───────────\n")

    migrated, already = migrate_to_global_headers(
        dry_run=dry_run,
        force=force,
    )

    if not dry_run and migrated > 0:
        from .reload import print_reload_hint
        print_reload_hint()


def _configure_stdio() -> None:
    """Use UTF-8 stdout/stderr on Windows to avoid cp1252 encode crashes."""
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                try:
                    reconfigure(encoding="utf-8", errors="replace")
                except Exception:
                    pass


def _launch_gui() -> None:
    """Open the desktop GUI."""
    try:
        from .gui.app import main as gui_main
    except ImportError as exc:
        print(
            "Error: GUI dependencies not installed.\n"
            "Run: uv tool install --force .\n"
            f"({exc})",
            file=sys.stderr,
        )
        sys.exit(1)
    gui_main()


def cmd_gui(args):
    """Open the desktop GUI."""
    _launch_gui()


def main():
    _configure_stdio()
    if len(sys.argv) == 1:
        _launch_gui()
        return
    if len(sys.argv) == 2 and sys.argv[1] in ("gui", "--gui"):
        _launch_gui()
        return

    parser = argparse.ArgumentParser(
        prog="cursaves",
        description=(
            "Sync Cursor agent chat sessions and global profile between machines. "
            "Run 'cursaves' with no arguments to open the graphical interface."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"cursaves {__version__}"
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Helper to add -w and -p flags to a subparser
    def add_project_args(p):
        p.add_argument(
            "--workspace", "-w",
            help="Workspace number, hash, or path substring from 'cursaves workspaces'",
        )
        p.add_argument("--project", "-p", help="Project path (default: current directory)")

    # ── init ────────────────────────────────────────────────────────
    p_init = subparsers.add_parser(
        "init", help="Initialize sync (git repo, S3 bucket, etc.)"
    )
    p_init.add_argument(
        "--remote", "-r",
        help="Git remote URL (e.g., git@github.com:you/my-saves.git)",
    )
    p_init.add_argument(
        "--backend", "-b",
        choices=["git", "s3"],
        help="Sync backend to use (default: git)",
    )
    p_init.add_argument(
        "--bucket",
        help="S3 bucket name (required for --backend s3)",
    )
    p_init.add_argument(
        "--prefix",
        help="S3 key prefix (default: snapshots/)",
    )
    p_init.add_argument(
        "--region",
        help="AWS region for S3 bucket",
    )
    p_init.add_argument(
        "--git-name",
        help="Git user.name for sync repo commits",
    )
    p_init.add_argument(
        "--git-email",
        help="Git user.email for sync repo commits",
    )
    p_init.add_argument(
        "--git-sign",
        action="store_true",
        default=None,
        help="Sign sync commits with GPG (default: disabled for sync repo)",
    )
    p_init.set_defaults(func=cmd_init)

    # ── setup ─────────────────────────────────────────────────────
    p_setup = subparsers.add_parser(
        "setup", help="Interactive first-time setup"
    )
    p_setup.add_argument(
        "--remote", "-r",
        help="Git remote URL for sync data (skips prompt)",
    )
    p_setup.add_argument(
        "--backend", "-b",
        choices=["git", "s3"],
        help="Sync backend (default: git)",
    )
    p_setup.add_argument(
        "--bucket",
        help="S3 bucket name (required for --backend s3)",
    )
    p_setup.add_argument(
        "--region",
        help="AWS region for S3 bucket",
    )
    p_setup.add_argument(
        "--yes", "-y", action="store_true",
        help="Use defaults for profile sync, initial sync, and auto-watch prompts",
    )
    p_setup.add_argument(
        "--no-sync", action="store_true",
        help="Skip initial sync after init",
    )
    p_setup.add_argument(
        "--no-watch", action="store_true",
        help="Skip auto-sync installation / instructions",
    )
    p_setup.add_argument(
        "--git-name",
        help="Git user.name for sync repo commits",
    )
    p_setup.add_argument(
        "--git-email",
        help="Git user.email for sync repo commits",
    )
    p_setup.add_argument(
        "--git-sign",
        action="store_true",
        default=None,
        help="Sign sync commits with GPG (default: disabled for sync repo)",
    )
    p_setup.set_defaults(func=cmd_setup)

    # ── config ────────────────────────────────────────────────────
    p_config = subparsers.add_parser("config", help="Configure cursaves settings")
    config_sub = p_config.add_subparsers(dest="config_command")

    p_config_git = config_sub.add_parser(
        "git", help="Configure git identity for sync repo commits"
    )
    p_config_git.add_argument(
        "--name",
        help="Git user.name for sync commits",
    )
    p_config_git.add_argument(
        "--email",
        help="Git user.email for sync commits",
    )
    p_config_git.add_argument(
        "--sign",
        action="store_true",
        default=None,
        help="Enable GPG signing for sync commits",
    )
    p_config_git.add_argument(
        "--no-sign",
        action="store_true",
        help="Disable GPG signing for sync commits",
    )
    p_config_git.add_argument(
        "--show",
        action="store_true",
        help="Show current git identity settings",
    )
    p_config_git.set_defaults(func=cmd_config)

    p_config_sync = config_sub.add_parser(
        "sync", help="Configure sync lifecycle (retention, pins, exclusions)"
    )
    p_config_sync.add_argument(
        "--retention-days",
        type=int,
        default=None,
        help="Prune snapshot files older than N days before push (0 = off, default 90)",
    )
    p_config_sync.add_argument(
        "--retention-purge-local",
        action="store_true",
        default=None,
        help="Also purge local Cursor DB when retention expires a chat",
    )
    p_config_sync.add_argument(
        "--no-retention-purge-local",
        action="store_false",
        dest="retention_purge_local",
        help="Do not purge local chats on retention (default)",
    )
    p_config_sync.add_argument(
        "--show",
        action="store_true",
        help="Show current sync lifecycle settings",
    )
    p_config_sync.add_argument(
        "--chat-enabled",
        action="store_true",
        default=None,
        dest="chat_enabled",
        help="Enable automatic chat sync in cursaves sync / watch",
    )
    p_config_sync.add_argument(
        "--no-chat-enabled",
        action="store_false",
        dest="chat_enabled",
        help="Disable automatic chat sync (default)",
    )
    p_config_sync.set_defaults(func=cmd_config)

    # ── remove ────────────────────────────────────────────────────
    p_remove = subparsers.add_parser(
        "remove",
        help="Remove chats from sync repo and Cursor (never sync again)",
    )
    p_remove.add_argument("--id", help="Composer / snapshot ID to remove")
    p_remove.add_argument(
        "--ids",
        help="Comma-separated composer IDs",
    )
    p_remove.add_argument(
        "--snapshot-only",
        action="store_true",
        help="Remove snapshots only; keep chats in Cursor",
    )
    p_remove.add_argument(
        "--force",
        action="store_true",
        help="Skip Cursor-running check when purging local chats",
    )
    p_remove.add_argument(
        "-y", "--yes",
        action="store_true",
        help="Skip confirmation prompt",
    )
    p_remove.set_defaults(func=cmd_remove)

    # ── pin ───────────────────────────────────────────────────────
    p_pin = subparsers.add_parser(
        "pin", help="Pin or unpin chats (pinned = exempt from retention)"
    )
    p_pin.add_argument("--id", required=True, help="Composer ID")
    p_pin.add_argument(
        "--unpin",
        action="store_true",
        help="Remove pin instead of adding",
    )
    p_pin.set_defaults(func=cmd_pin)

    # ── auth ──────────────────────────────────────────────────────
    p_auth = subparsers.add_parser("auth", help="Authenticate with external services")
    auth_sub = p_auth.add_subparsers(dest="auth_command")

    p_auth_github = auth_sub.add_parser(
        "github", help="Login with GitHub (push/pull auth + commit identity + remote)"
    )
    p_auth_github.add_argument(
        "--status", action="store_true",
        help="Show GitHub login and sync remote status",
    )
    p_auth_github.add_argument(
        "--logout", action="store_true",
        help="Log out from GitHub",
    )
    p_auth_github.add_argument(
        "--login-only", action="store_true",
        help="Login and save identity only, do not configure remote",
    )
    p_auth_github.add_argument(
        "--remote", "-r",
        help="Use an existing GitHub HTTPS remote URL",
    )
    p_auth_github.add_argument(
        "--create-repo", action="store_true",
        help="Create private cursaves-data repo on your account",
    )
    p_auth_github.add_argument(
        "--yes", "-y", action="store_true",
        help="Non-interactive: auto-create repo if needed",
    )
    p_auth_github.set_defaults(func=cmd_auth)

    # ── workspaces ─────────────────────────────────────────────────
    p_workspaces = subparsers.add_parser(
        "workspaces", help="List all Cursor workspaces (local and SSH remote)"
    )
    p_workspaces.set_defaults(func=cmd_workspaces)

    # ── snapshots ──────────────────────────────────────────────────
    p_snapshots = subparsers.add_parser(
        "snapshots", help="List snapshot projects available in ~/.cursaves/"
    )
    p_snapshots.set_defaults(func=cmd_snapshots)

    # ── list ────────────────────────────────────────────────────────
    p_list = subparsers.add_parser("list", help="List conversations for a project")
    add_project_args(p_list)
    p_list.add_argument("--json", action="store_true", help="Output as JSON for scripting")
    p_list.set_defaults(func=cmd_list)

    # ── export ──────────────────────────────────────────────────────
    p_export = subparsers.add_parser("export", help="Export a single conversation")
    p_export.add_argument("id", help="Conversation (composer) ID")
    add_project_args(p_export)
    p_export.set_defaults(func=cmd_export)

    # ── checkpoint ──────────────────────────────────────────────────
    p_checkpoint = subparsers.add_parser(
        "checkpoint", help="Export all conversations for a project"
    )
    add_project_args(p_checkpoint)
    p_checkpoint.set_defaults(func=cmd_checkpoint)

    # ── import ──────────────────────────────────────────────────────
    p_import = subparsers.add_parser("import", help="Import conversation snapshots")
    p_import.add_argument("--all", action="store_true", help="Import all snapshots for the project")
    p_import.add_argument("--file", "-f", help="Import a specific snapshot file")
    add_project_args(p_import)
    p_import.add_argument(
        "--force", action="store_true",
        help="Suppress the Cursor-running warning",
    )
    p_import.add_argument(
        "--reload", action="store_true",
        help="(deprecated, no effect) Cursor requires a full restart to see imports",
    )
    p_import.set_defaults(func=cmd_import)

    # ── push ────────────────────────────────────────────────────────
    p_push = subparsers.add_parser(
        "push", help="Checkpoint + commit + push (one command to save and sync)"
    )
    add_project_args(p_push)
    p_push.add_argument(
        "--select", "-s", action="store_true",
        help="Interactively select workspace first",
    )
    p_push.add_argument(
        "--all", dest="all_chats", action="store_true",
        help="Push all conversations without selection prompt",
    )
    p_push.add_argument(
        "--ahead", "-a", action="store_true",
        help="Find and push all conversations ahead of snapshots across all workspaces",
    )
    p_push.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print git commands and output during push",
    )
    p_push.set_defaults(func=cmd_push)

    # ── pull ────────────────────────────────────────────────────────
    p_pull = subparsers.add_parser(
        "pull", help="Git pull + import snapshots (one command to sync and restore)"
    )
    p_pull.add_argument(
        "--workspace", "-w",
        help="Target workspace to import into (number, hash, or path substring from 'cursaves workspaces')",
    )
    p_pull.add_argument("--project", "-p", help="Project path (default: current directory)")
    p_pull.add_argument(
        "--select", "-s", action="store_true",
        help="Interactively select which snapshot projects to import",
    )
    p_pull.add_argument(
        "--force", action="store_true",
        help="Suppress the Cursor-running warning",
    )
    p_pull.add_argument(
        "--reload", action="store_true",
        help="(deprecated, no effect) Cursor requires a full restart to see imports",
    )
    p_pull.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print git commands and output during pull",
    )
    p_pull.set_defaults(func=cmd_pull)

    # ── sync ──────────────────────────────────────────────────────
    p_sync = subparsers.add_parser(
        "sync", help="Pull behind + push ahead — one command to stay in sync across machines"
    )
    p_sync.add_argument(
        "--no-profile", action="store_true",
        help="Skip global Cursor profile sync (settings, skills, hooks, etc.)",
    )
    p_sync.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print git commands and output during sync",
    )
    p_sync.set_defaults(func=cmd_sync)

    # ── profile ───────────────────────────────────────────────────
    p_profile = subparsers.add_parser(
        "profile", help="Sync global Cursor config (settings, skills, commands, hooks)"
    )
    profile_sub = p_profile.add_subparsers(dest="profile_command")

    p_profile_push = profile_sub.add_parser(
        "push", help="Export local Cursor config and push to remote"
    )
    p_profile_push.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print git commands and output",
    )
    p_profile_push.set_defaults(func=cmd_profile_push)

    p_profile_pull = profile_sub.add_parser(
        "pull", help="Pull remote profile and apply to this machine"
    )
    p_profile_pull.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print git commands and output",
    )
    p_profile_pull.set_defaults(func=cmd_profile_pull)

    p_profile_status = profile_sub.add_parser(
        "status", help="Show local vs profile mirror status"
    )
    p_profile_status.set_defaults(func=cmd_profile_status)

    # ── skills ────────────────────────────────────────────────────
    p_skills = subparsers.add_parser("skills", help="List or delete synced skills")
    skills_sub = p_skills.add_subparsers(dest="skills_command")

    p_skills_list = skills_sub.add_parser("list", help="List skills in sync repo")
    p_skills_list.set_defaults(func=cmd_skills)

    p_skills_delete = skills_sub.add_parser("delete", help="Delete a skill from local and repo")
    p_skills_delete.add_argument("name", help="Skill directory name")
    p_skills_delete.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation prompt"
    )
    p_skills_delete.set_defaults(func=cmd_skills)

    # ── hooks ─────────────────────────────────────────────────────
    p_hooks = subparsers.add_parser("hooks", help="List or delete synced hooks")
    hooks_sub = p_hooks.add_subparsers(dest="hooks_command")

    p_hooks_list = hooks_sub.add_parser("list", help="List hooks in sync repo")
    p_hooks_list.set_defaults(func=cmd_hooks)

    p_hooks_delete = hooks_sub.add_parser("delete", help="Delete a hook from local and repo")
    p_hooks_delete.add_argument(
        "name", help="Hook script filename or hooks.json command path"
    )
    p_hooks_delete.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation prompt"
    )
    p_hooks_delete.set_defaults(func=cmd_hooks)

    # ── repair ─────────────────────────────────────────────────────
    p_repair = subparsers.add_parser(
        "repair", help="Restore missing agent blobs from snapshots (fixes 'Blob not found' errors)"
    )
    p_repair.set_defaults(func=cmd_repair)

    # ── reload ─────────────────────────────────────────────────────
    p_reload = subparsers.add_parser(
        "reload", help="(deprecated) Print restart instructions"
    )
    p_reload.set_defaults(func=cmd_reload)

    # ── delete ─────────────────────────────────────────────────────
    p_delete = subparsers.add_parser(
        "delete", help="Delete cached snapshots"
    )
    p_delete.add_argument("--project", "-p", help="Project path (default: current directory)")
    p_delete.add_argument("--all", action="store_true", help="Delete all snapshots for the project")
    p_delete.add_argument("--id", help="Delete a specific snapshot by ID (supports partial match)")
    p_delete.add_argument(
        "--select", "-s", action="store_true",
        help="Interactively select which project(s) to delete",
    )
    p_delete.add_argument(
        "--all-projects", action="store_true",
        help="Delete ALL snapshots across ALL projects",
    )
    p_delete.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip confirmation prompt",
    )
    p_delete.set_defaults(func=cmd_delete)

    # ── copy ───────────────────────────────────────────────────────
    p_copy = subparsers.add_parser(
        "copy", help="Copy conversations between workspaces (same machine)"
    )
    p_copy.add_argument(
        "--force", action="store_true",
        help="Suppress the Cursor-running warning",
    )
    p_copy.set_defaults(func=cmd_copy)

    # ── status ──────────────────────────────────────────────────────
    p_status = subparsers.add_parser("status", help="Show sync status")
    add_project_args(p_status)
    p_status.set_defaults(func=cmd_status)

    # ── watch ────────────────────────────────────────────────────────
    p_watch = subparsers.add_parser(
        "watch", help="Auto-sync all workspaces while Cursor is open"
    )
    p_watch.add_argument(
        "--all", dest="watch_all", action="store_true",
        help="Sync all workspaces (required to run watch)",
    )
    p_watch.add_argument(
        "--install-hook", action="store_true",
        help="Install Cursor sessionStart hook to run watch --all when Cursor opens",
    )
    p_watch.add_argument(
        "--uninstall-hook", action="store_true",
        help="Remove the Cursor auto-sync hook",
    )
    p_watch.add_argument(
        "--detach", action="store_true",
        help="Start watch --all in background (used by hook; no console window)",
    )
    p_watch.add_argument(
        "--poll-interval", type=int, default=POLL_INTERVAL_DEFAULT,
        help=f"Seconds between Cursor open/close checks (default: {POLL_INTERVAL_DEFAULT})",
    )
    p_watch.add_argument(
        "--interval", "-i", type=int, default=60,
        help="Seconds between sync cycles while Cursor is open (default: 60, or 120 with --all)",
    )
    p_watch.add_argument(
        "--no-git", action="store_true",
        help="Disable automatic git commit/push",
    )
    p_watch.add_argument("--verbose", "-v", action="store_true", help="Print on every check")
    p_watch.set_defaults(func=cmd_watch)

    # ── gui ─────────────────────────────────────────────────────────
    p_gui = subparsers.add_parser(
        "gui", help="Open the graphical interface (default when run with no args)"
    )
    p_gui.set_defaults(func=cmd_gui)

    # ── doctor ─────────────────────────────────────────────────────
    p_doctor = subparsers.add_parser(
        "doctor", help="Audit chats and recover orphaned conversations"
    )
    p_doctor.add_argument(
        "--recover", action="store_true",
        help="Re-register orphaned chats in their workspaces",
    )
    p_doctor.add_argument(
        "--select", "-s", action="store_true",
        help="Interactively select which orphaned chats to recover",
    )
    p_doctor.add_argument(
        "--force", action="store_true",
        help="Skip the Cursor-running check (use if you can't fully quit Cursor)",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    # ── move-chat ──────────────────────────────────────────────────
    p_move = subparsers.add_parser(
        "move-chat",
        help="Re-tag chats to a different workspace (change workspaceIdentifier)",
    )
    p_move.add_argument(
        "--to", required=True,
        help="Destination workspace: hash, index, path substring, or 'unassigned'",
    )
    p_move.add_argument(
        "--from", dest="from_ws",
        help="Move ALL chats currently tagged to this source workspace",
    )
    p_move.add_argument(
        "--id",
        help="Move a specific chat by (partial) composer id",
    )
    p_move.add_argument(
        "--dry-run", action="store_true",
        help="Show what would move without writing",
    )
    p_move.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the confirmation prompt",
    )
    p_move.add_argument(
        "--force", action="store_true",
        help="Skip the Cursor-running check",
    )
    p_move.set_defaults(func=cmd_move_chat)

    p_migrate = subparsers.add_parser(
        "migrate", help="Migrate old chats to Cursor 3.0 global index"
    )
    p_migrate.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be migrated without writing",
    )
    p_migrate.add_argument(
        "--force", action="store_true",
        help="Skip the Cursor-running check",
    )
    p_migrate.set_defaults(func=cmd_migrate)

    p_purge = subparsers.add_parser(
        "purge", help="Delete chats from Cursor's database to reclaim space"
    )
    p_purge.add_argument(
        "--workspace", "-w",
        help="Filter to chats from a specific workspace (name substring)",
    )
    p_purge.add_argument(
        "--force", action="store_true",
        help="Skip the Cursor-running check",
    )
    p_purge.set_defaults(func=cmd_purge)

    args = parser.parse_args()
    if args.command == "profile" and not getattr(args, "profile_command", None):
        p_profile.print_help()
        sys.exit(1)
    if args.command == "config" and not getattr(args, "config_command", None):
        p_config.print_help()
        sys.exit(1)
    if not args.command:
        print(
            "cursaves - sync Cursor chats and profile between machines\n"
            "\n"
            "Usage: cursaves <command> [options]\n"
            "\n"
            "─── Quick start ────────────────────────────────────────────────\n"
            "\n"
            "  (no args)             Open graphical interface (default)\n"
            "  gui                   Open graphical interface\n"
            "\n"
            "─── Sync between machines ──────────────────────────────────────\n"
            "\n"
            "  setup                 Interactive first-time setup\n"
            "  init                  Initialize ~/.cursaves/ sync repo\n"
            "  init -r <url>         Initialize with git remote URL\n"
            "  push                  Save + commit + push chats\n"
            "  push -s               Select workspace + chats to push\n"
            "  pull                  Pull + import chats\n"
            "  pull -s               Select which snapshots to import\n"
            "  profile push          Export settings/skills/commands and push\n"
            "  profile pull          Pull profile and apply to this machine\n"
            "  profile status        Show profile sync status\n"
            "\n"
            "─── Copy between workspaces (same machine) ─────────────────────\n"
            "\n"
            "  copy                  Copy chats between workspaces\n"
            "  move-chat --to <ws>   Re-tag chats to another workspace\n"
            "\n"
            "─── Info & management ──────────────────────────────────────────\n"
            "\n"
            "  workspaces            List Cursor workspaces (local + SSH)\n"
            "  list                  List chats for this project\n"
            "  snapshots             List saved snapshots in ~/.cursaves/\n"
            "  status                Show synced vs local-only chats\n"
            "  doctor                Audit chats, find orphaned conversations\n"
            "  doctor --recover      Re-register orphaned chats in workspaces\n"
            "  migrate               Migrate old chats to Cursor 3.0 index\n"
            "  migrate --dry-run     Preview migration without writing\n"
            "  purge                 Delete chats from Cursor DB to free space\n"
            "  purge -w <name>       Filter purge to a specific workspace\n"
            "  delete -s             Select which snapshots to delete\n"
            "  delete --all-projects Delete ALL snapshots\n"
            "\n"
            "─── Options ─────────────────────────────────────────────────────\n"
            "\n"
            "  -w <number>           Target workspace # (from 'workspaces')\n"
            "  -p <path>             Target project path\n"
            "  -s, --select          Interactive selection mode\n"
            "  -y, --yes             Skip confirmation prompts\n"
            "\n"
            "After importing, restart Cursor (quit + reopen) to see chats.\n"
            "Profile changes (settings, skills, hooks) may also require a restart.\n"
            "\n"
            "Run 'cursaves <command> --help' for more options.\n"
            "Update: uv tool upgrade cursaves"
        )
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
