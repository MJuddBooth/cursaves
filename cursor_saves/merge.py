"""Reconcile two copies of the same conversation.

Two chats that descend from one conversation share a leading run of messages
(identified by ``serverBubbleId``) and then branch. Cursor builds what the
agent continues from out of an encoded ``conversationState`` plus content
blobs, not out of the displayed messages, so the two branches cannot be
interleaved into one chat that the model would understand. Two operations are
supported instead, each with honest semantics:

``context``
    Render the messages that exist only in the source branch as a readable
    markdown transcript, for attaching to the target chat as reference
    material. Nothing in Cursor's databases is touched.

``replace``
    Make the target chat the source: its messages, agent state and blobs are
    overwritten in place, so continuing the chat continues the source
    branch. The target's previous version is first kept as a separate chat.
"""

import copy
import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import db, importer, paths

SUMMARY_WIDTH = 160


class MergeError(Exception):
    """A merge cannot proceed; the message is meant for the user."""


# ── Locating chats ───────────────────────────────────────────────────────


def resolve_chat_id(selector: str, cdb: "db.CursorDB") -> str:
    """Resolve a full or unique-prefix composer ID against the global DB."""
    selector = selector.strip().lower()
    ids = [
        k[len("composerData:"):]
        for k in cdb.list_keys("composerData:", table="cursorDiskKV")
    ]
    matches = [i for i in ids if i.lower() == selector] or [
        i for i in ids if i.lower().startswith(selector)
    ]
    if not matches:
        raise MergeError(f"No chat matches '{selector}'.")
    if len(matches) > 1:
        raise MergeError(
            f"'{selector}' is ambiguous: " + ", ".join(m[:12] for m in matches[:6])
        )
    return matches[0]


def _workspace_label(composer_id: str) -> tuple[str, Optional[Path], Optional[dict]]:
    """Return (display name, workspace dir, workspace dict) for a chat."""
    ws_id = None
    for candidate, entries in paths._build_global_headers_map().items():
        if any(e.get("composerId") == composer_id for e in entries):
            ws_id = candidate
            break
    if not ws_id or ws_id == paths.UNASSIGNED_WS_ID:
        return "unassigned", None, None
    ws_dir = paths.get_workspace_storage_dir() / ws_id
    for ws in paths.list_all_workspaces():
        if ws["workspace_dir"].name == ws_id:
            return ws.get("name") or ws["path"], ws_dir, ws
    return ws_id[:8], ws_dir, None


def load_chat_source(selector: str, cdb: "db.CursorDB") -> dict:
    composer_id = resolve_chat_id(selector, cdb)
    body = cdb.get_json(f"composerData:{composer_id}")
    if not body:
        raise MergeError(f"Chat {composer_id[:8]} has no body in the global DB.")
    where, _, _ = _workspace_label(composer_id)
    return {
        "kind": "chat",
        "id": composer_id,
        "name": body.get("name") or "(unnamed)",
        "headers": body.get("fullConversationHeadersOnly") or [],
        "where": where,
        "origin": f"chat in {where}",
        "composerData": body,
        "snapshot": None,
    }


def load_snapshot_source(selector: str) -> dict:
    """Load a snapshot by file path, or by composer ID (prefix) in ~/.cursaves."""
    path = Path(selector).expanduser()
    if path.exists() and path.is_file():
        files = [path]
    else:
        files = []
        root = paths.get_snapshots_dir()
        for project in (p for p in root.iterdir() if p.is_dir()) if root.exists() else []:
            for f in importer.list_snapshot_files(project):
                stem = f.name.split(".")[0]
                if stem.lower().startswith(selector.strip().lower()):
                    files.append(f)
    if not files:
        raise MergeError(f"No snapshot matches '{selector}'.")

    snapshots = []
    for f in files:
        meta = importer.read_snapshot_meta(f)
        snapshots.append((
            meta.get("exportedAt") or "",
            meta.get("composerId") or "",
            f,
            meta.get("sourceMachine") or "?",
            meta.get("messageCount") or 0,
        ))
    if len({s[1] for s in snapshots}) > 1:
        raise MergeError(
            f"'{selector}' matches several chats: "
            + ", ".join(sorted({s[1][:12] for s in snapshots}))
        )
    # The same chat can be filed under several project folders. Identical
    # copies (same machine and message count) are interchangeable; differing
    # ones are a real choice, so do not guess.
    if len({(s[3], s[4]) for s in snapshots}) > 1:
        listing = "\n".join(
            f"  {s[3]}, {s[4]} messages, exported {s[0][:16]}: {s[2]}"
            for s in sorted(snapshots, reverse=True)
        )
        raise MergeError(
            f"'{selector}' has several differing snapshots; pass one by file path:\n{listing}"
        )
    exported_at, _, chosen, _, _ = max(snapshots)
    snap = importer.read_snapshot_file(chosen)
    body = snap.get("composerData") or {}
    machine = snap.get("sourceHost") or snap.get("sourceMachine") or "?"
    return {
        "kind": "snapshot",
        "id": snap.get("composerId") or body.get("composerId"),
        "name": body.get("name") or "(unnamed)",
        "headers": body.get("fullConversationHeadersOnly") or [],
        "where": machine,
        "origin": f"snapshot from {machine} ({exported_at[:16]})",
        "composerData": body,
        "snapshot": snap,
        "snapshot_path": chosen,
    }


# ── Planning ─────────────────────────────────────────────────────────────


def plan_merge(target_headers: list, source_headers: list) -> dict:
    """Compare two header lists and pick out the source-only messages."""
    info = importer.describe_divergence(target_headers, source_headers)
    if not info["comparable"]:
        raise MergeError(
            "One of these chats has no server message IDs, so their common "
            "history cannot be determined."
        )
    if info["ancestor"] == 0:
        raise MergeError("These chats share no messages; they are unrelated.")

    target_ids = set(importer._server_bubble_ids(target_headers))
    first = next(
        (
            i
            for i, h in enumerate(source_headers)
            if h.get("serverBubbleId") and h["serverBubbleId"] not in target_ids
        ),
        None,
    )
    tail = (
        []
        if first is None
        else [h for h in source_headers[first:] if h.get("serverBubbleId") not in target_ids]
    )

    target_only = info["local_only"]
    if not tail and not target_only:
        relation = "identical"
    elif not tail:
        relation = "source is behind target (nothing new in it)"
    elif not target_only:
        relation = "target is behind source (a fast-forward)"
    else:
        relation = "diverged"
    return {**info, "tail": tail, "relation": relation, "target_only": target_only}


# ── Context mode ─────────────────────────────────────────────────────────


def _one_line(text: str, width: int = SUMMARY_WIDTH) -> str:
    flat = re.sub(r"\s+", " ", str(text)).strip()
    return flat if len(flat) <= width else flat[: width - 3] + "..."


def _classify(bubble: dict) -> Optional[tuple[str, str]]:
    """Return (kind, text) for a displayable bubble, or None to skip it."""
    tool = bubble.get("toolFormerData")
    if isinstance(tool, dict) and tool.get("name"):
        summary = tool.get("params") or tool.get("rawArgs") or ""
        status = tool.get("status")
        suffix = f" [{status}]" if status and status != "completed" else ""
        return "tool", f"`{tool['name']}` {_one_line(summary)}{suffix}"
    text = (bubble.get("text") or "").strip()
    if not text:
        return None  # thinking-only and empty bubbles
    return ("user" if bubble.get("type") == 1 else "assistant"), text


def render_context(
    plan: dict,
    source: dict,
    target: dict,
    get_bubble: Callable[[str], Optional[dict]],
) -> tuple[str, int]:
    """Render the source-only messages as markdown. Returns (text, messages)."""
    blocks: list[tuple[str, str]] = []
    for header in plan["tail"]:
        bubble = get_bubble(header.get("bubbleId", ""))
        if not bubble:
            continue
        item = _classify(bubble)
        if item:
            blocks.append(item)

    lines = [
        f"# Context from another branch of \"{source['name']}\"",
        "",
        f"- Source: {source['origin']}, chat `{source['id']}`",
        f"- Target chat: \"{target['name']}\" (`{target['id']}`)",
        f"- Shared history: the first {plan['ancestor']} messages, which the target already has",
        f"- Below: the {len(plan['tail'])} messages that exist only in the source branch "
        f"(thinking text and tool results are left out)",
        "",
        "This is reference material from a parallel branch of the conversation. "
        "It did not happen in the target chat, and its edits may not exist in "
        "this checkout.",
        "",
        "---",
        "",
    ]

    count = 0
    run_of_tools = False
    for kind, text in blocks:
        if kind == "tool":
            if not run_of_tools:
                lines.append("*Tool calls:*")
                run_of_tools = True
            lines.append(f"- {text}")
            continue
        if run_of_tools:
            lines.append("")
            run_of_tools = False
        count += 1
        lines += [f"## {count}. {'User' if kind == 'user' else 'Assistant'}", "", text, ""]
    if run_of_tools:
        lines.append("")
    return "\n".join(lines), count


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()[:40] or "chat"


def default_context_dir() -> Path:
    return paths.get_sync_dir() / "context"


def write_context(
    target: dict,
    source: dict,
    plan: dict,
    get_bubble: Callable[[str], Optional[dict]],
    out_dir: Optional[Path] = None,
) -> tuple[Path, int]:
    text, count = render_context(plan, source, target, get_bubble)
    out_dir = Path(out_dir) if out_dir else default_context_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / (
        f"{_slug(source['name'])}--{source['id'][:8]}-into-{target['id'][:8]}.md"
    )
    out.write_text(text, encoding="utf-8")
    return out, count


# ── Replace mode ─────────────────────────────────────────────────────────

_PER_CHAT_PREFIXES = ("bubbleId:", "checkpointId:", "messageRequestContext:")


def _read_prefixed(cdb: "db.CursorDB", prefix: str) -> dict:
    out = {}
    for key in cdb.list_keys(prefix):
        value = cdb.get_json(key)
        if value:
            out[key[len(prefix):]] = value
    return out


def _chat_payload(cdb: "db.CursorDB", composer_id: str) -> dict:
    return {
        "bubbles": _read_prefixed(cdb, f"bubbleId:{composer_id}:"),
        "checkpoints": _read_prefixed(cdb, f"checkpointId:{composer_id}:"),
        "contexts": _read_prefixed(cdb, f"messageRequestContext:{composer_id}:"),
        "contentBlobs": {},
        "agentBlobs": {},
    }


def _snapshot_payload(snap: dict) -> dict:
    return {
        "bubbles": snap.get("bubbleEntries") or {},
        "checkpoints": snap.get("checkpoints") or {},
        "contexts": snap.get("messageContexts") or {},
        "contentBlobs": snap.get("contentBlobs") or {},
        "agentBlobs": snap.get("agentBlobs") or {},
    }


def _write_payload(wcdb: "db.CursorDB", composer_id: str, payload: dict) -> None:
    import base64

    if payload["contentBlobs"]:
        wcdb.write_batch(
            [(f"composer.content.{h}", v) for h, v in payload["contentBlobs"].items()]
        )
    if payload["contexts"]:
        wcdb.write_json_batch([
            (f"messageRequestContext:{composer_id}:{k}", v)
            for k, v in payload["contexts"].items()
        ])
    if payload["bubbles"]:
        wcdb.write_json_batch([
            (f"bubbleId:{composer_id}:{k}", v) for k, v in payload["bubbles"].items()
        ])
    if payload["checkpoints"]:
        wcdb.write_json_batch([
            (f"checkpointId:{composer_id}:{k}", v)
            for k, v in payload["checkpoints"].items()
        ])
    if payload["agentBlobs"]:
        wcdb.write_batch([
            (f"agentKv:blob:{bid}", base64.b64decode(data))
            for bid, data in payload["agentBlobs"].items()
        ])


def _clone_chat(
    rcdb: "db.CursorDB",
    wcdb: "db.CursorDB",
    composer_id: str,
    new_name: str,
    ws_dir: Path,
) -> str:
    """Duplicate a chat under a new ID and register it in the same workspace."""
    body = rcdb.get_json(f"composerData:{composer_id}")
    new_id = str(uuid.uuid4())
    clone = copy.deepcopy(body)
    clone["composerId"] = new_id
    clone["name"] = new_name
    wcdb.write_json(f"composerData:{new_id}", clone)
    _write_payload(wcdb, new_id, _chat_payload(rcdb, composer_id))
    if not importer._register_in_workspace(new_id, clone, ws_dir):
        raise MergeError("Could not register the preserved copy in the workspace.")
    return new_id


def _update_header_row(wcdb: "db.CursorDB", composer_id: str, body: dict) -> None:
    """Refresh the sidebar row in place, keeping its archive state and workspace."""
    for row in wcdb.read_composer_headers():
        if row.get("composerId") != composer_id:
            continue
        updated = body.get("lastUpdatedAt") or row.get("lastUpdatedAt")
        row["lastUpdatedAt"] = updated
        row["recency"] = updated
        value = row.get("value") or {}
        value["lastUpdatedAt"] = updated
        value["subtitle"] = body.get("subtitle", value.get("subtitle", ""))
        value["totalLinesAdded"] = body.get("totalLinesAdded", 0)
        value["totalLinesRemoved"] = body.get("totalLinesRemoved", 0)
        value["filesChangedCount"] = body.get("filesChangedCount", 0)
        row["value"] = value
        wcdb.upsert_composer_header(row)
        return


def replace_chat(target: dict, source: dict, force: bool = False) -> dict:
    """Overwrite the target chat with the source, keeping the old version."""
    if not force and importer.is_cursor_running():
        raise MergeError(
            "Cursor is running. Close Cursor first (Cmd+Q / quit), then re-run.\n"
            "Use --force to override (not recommended)."
        )

    global_db = paths.get_global_db_path()
    where, ws_dir, ws = _workspace_label(target["id"])
    if ws_dir is None or not (ws_dir / "state.vscdb").exists():
        raise MergeError(
            f"The target's workspace ({where}) has no workspace database to register "
            "the preserved copy in."
        )

    backup = db.backup_db(global_db)
    print(f"  Backed up global DB to {backup.name}")

    rcdb = db.CursorDB(global_db)
    wcdb = db.CursorDB(global_db)
    try:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        copy_name = f"{target['name']} (replaced {stamp})"
        copy_id = _clone_chat(rcdb, wcdb, target["id"], copy_name, ws_dir)
        print(f"  Kept previous version as \"{copy_name}\" ({copy_id[:8]})")

        if source["kind"] == "chat":
            payload = _chat_payload(rcdb, source["id"])
        else:
            payload = _snapshot_payload(source["snapshot"])

        new_body = copy.deepcopy(source["composerData"])
        if source["kind"] == "snapshot" and ws is not None:
            source_path = os.path.normpath(
                source["snapshot"].get("sourceProjectPath") or ""
            )
            target_path = os.path.normpath(ws["path"])
            if source_path and source_path != target_path:
                new_body = importer.rewrite_paths(new_body, source_path, target_path)
                payload["bubbles"] = {
                    k: importer.rewrite_paths(v, source_path, target_path)
                    for k, v in payload["bubbles"].items()
                }
                payload["checkpoints"] = {
                    k: importer.rewrite_paths(v, source_path, target_path)
                    for k, v in payload["checkpoints"].items()
                }
                print(f"  Rewrote paths: {source_path} -> {target_path}")

        target_body = rcdb.get_json(f"composerData:{target['id']}") or {}
        new_body["composerId"] = target["id"]
        new_body["name"] = target_body.get("name", target["name"])
        for key in ("workspaceIdentifier", "createdAt"):
            if target_body.get(key) is not None:
                new_body[key] = target_body[key]

        removed = 0
        for prefix in _PER_CHAT_PREFIXES:
            removed += wcdb.delete_keys_by_prefix(f"{prefix}{target['id']}:")
        wcdb.write_json(f"composerData:{target['id']}", new_body)
        _write_payload(wcdb, target["id"], payload)
        _update_header_row(wcdb, target["id"], new_body)
    finally:
        rcdb.close()
        wcdb.close()
    paths.invalidate_headers_cache()

    verify = db.CursorDB(global_db)
    try:
        written = verify.get_json(f"composerData:{target['id']}") or {}
        count = len(written.get("fullConversationHeadersOnly") or [])
        bubbles = len(verify.list_keys(f"bubbleId:{target['id']}:"))
    finally:
        verify.close()
    if count != len(source["headers"]):
        raise MergeError(
            f"Verification failed: wrote {count} messages, expected {len(source['headers'])}. "
            f"The previous version is preserved as {copy_id[:8]}; the DB backup is {backup.name}."
        )
    return {
        "copy_id": copy_id,
        "copy_name": copy_name,
        "messages": count,
        "bubbles": bubbles,
        "removed_keys": removed,
        "backup": backup.name,
    }
