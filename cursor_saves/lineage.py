"""Find chats that descend from the same conversation.

Cursor clones a chat when its workspace changes, and cursaves imports a
diverged copy as a separate chat, so one conversation can exist as several
records with different IDs and no marker linking them. Every message carries
a ``serverBubbleId`` that is stable across clones and machines, so chats that
start from the same first message belong to one lineage.
"""

from . import db, paths
from .importer import (
    _server_bubble_ids,
    list_snapshot_files,
    read_snapshot_file,
)


def _workspace_names() -> dict[str, str]:
    names = {}
    for ws in paths.list_all_workspaces():
        names[ws["workspace_dir"].name] = ws.get("name") or ws["path"]
    return names


def _collect_local(names: dict[str, str]) -> list[dict]:
    """Every chat body in the global DB, archived ones included."""
    global_db = paths.get_global_db_path()
    if not global_db.exists():
        return []

    meta: dict[str, dict] = {}
    for ws_id, entries in paths._build_global_headers_map().items():
        for entry in entries:
            cid = entry.get("composerId")
            if cid:
                meta[cid] = {**entry, "_ws": ws_id}

    members = []
    with db.CursorDB(global_db) as cdb:
        for key in cdb.list_keys("composerData:", table="cursorDiskKV"):
            cid = key[len("composerData:"):]
            body = cdb.get_json(key, table="cursorDiskKV")
            if not isinstance(body, dict):
                continue
            seq = _server_bubble_ids(body.get("fullConversationHeadersOnly"))
            info = meta.get(cid, {})
            ws_id = info.get("_ws", "")
            members.append({
                "id": cid,
                "name": body.get("name") or info.get("name") or "(unnamed)",
                "where": names.get(ws_id) or (ws_id[:8] if ws_id else "?"),
                "kind": "archived" if info.get("isArchived") else "live",
                "seq": seq,
                "updated": body.get("lastUpdatedAt") or info.get("lastUpdatedAt") or 0,
            })
    return members


def _collect_snapshots(local_seqs: dict[str, list[str]]) -> list[dict]:
    """Snapshot files that carry something the local chat of the same ID lacks."""
    members = []
    root = paths.get_snapshots_dir()
    if not root.exists():
        return members

    for project in sorted(p for p in root.iterdir() if p.is_dir()):
        for snap_path in list_snapshot_files(project):
            try:
                snap = read_snapshot_file(snap_path)
            except Exception:
                continue
            body = snap.get("composerData") or {}
            cid = snap.get("composerId") or body.get("composerId") or snap_path.stem
            seq = _server_bubble_ids(body.get("fullConversationHeadersOnly"))
            if cid in local_seqs and local_seqs[cid] == seq:
                continue
            members.append({
                "id": cid,
                "name": body.get("name") or "(unnamed)",
                "where": f"{project.name} snapshot",
                "kind": f"snapshot from {snap.get('sourceMachine') or '?'}",
                "seq": seq,
                "updated": 0,
            })
    return members


def _relation(member: dict, reference: dict) -> str:
    if member is reference:
        return "reference (most messages)"
    mine, theirs = set(member["seq"]), set(reference["seq"])
    own, missing = len(mine - theirs), len(theirs - mine)
    if not own and not missing:
        return "identical to reference"
    if not own:
        return f"behind reference by {missing} message(s)"
    return f"diverged: {own} own, {missing} missing from it"


def find_lineages(include_snapshots: bool = False) -> dict:
    """Group chats that share a conversation root.

    Returns ``{"families": [...], "unlinked": n}``. Each family lists its
    members (largest first) with their relation to the largest one, and the
    number of messages every member has in common.
    """
    members = _collect_local(_workspace_names())
    if include_snapshots:
        local_seqs = {m["id"]: m["seq"] for m in members}
        members += _collect_snapshots(local_seqs)

    by_root: dict[str, list[dict]] = {}
    unlinked = 0
    for m in members:
        if not m["seq"]:
            unlinked += 1
            continue
        by_root.setdefault(m["seq"][0], []).append(m)

    families = []
    for group in by_root.values():
        if len(group) < 2:
            continue
        group.sort(
            key=lambda m: (len(m["seq"]), m["kind"] == "live", m["updated"]),
            reverse=True,
        )
        reference = group[0]

        common = 0
        for column in zip(*(m["seq"] for m in group)):
            if len(set(column)) != 1:
                break
            common += 1

        families.append({
            "common_messages": common,
            "members": [
                {
                    "id": m["id"],
                    "name": m["name"],
                    "where": m["where"],
                    "kind": m["kind"],
                    "messages": len(m["seq"]),
                    "relation": _relation(m, reference),
                }
                for m in group
            ],
        })

    families.sort(key=lambda f: -len(f["members"]))
    return {"families": families, "unlinked": unlinked}
