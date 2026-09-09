"""Tools / maintenance tab."""

from __future__ import annotations

import customtkinter as ctk

from ...importer import (
    copy_between_workspaces,
    list_all_chats_with_sizes,
    move_chat,
    purge_chats,
)
from ..runner import CommandRunner
from ..widgets import ChatCheckList, WorkspaceSelector, confirm_action, warn_cursor_running


def build_tools(parent, runner: CommandRunner, log_append, require_sync_ready) -> None:
    frame = ctk.CTkScrollableFrame(parent, fg_color="transparent")
    frame.pack(fill="both", expand=True, padx=8, pady=8)

    ctk.CTkLabel(frame, text="Maintenance", font=ctk.CTkFont(weight="bold")).pack(
        anchor="w", padx=4, pady=(4, 8),
    )

    maint_frame = ctk.CTkFrame(frame, fg_color="transparent")
    maint_frame.pack(fill="x", pady=4)

    def run(args):
        if args[0] not in ("doctor", "repair", "migrate") and not require_sync_ready():
            return
        runner.run(CommandRunner.cursaves_argv(*args))

    def doctor_recover():
        if confirm_action("Doctor recover", "Re-register orphaned chats in workspaces?"):
            run(["doctor", "--recover"])

    def migrate():
        if warn_cursor_running("migrate") is False:
            return
        if confirm_action("Migrate", "Migrate chats to Cursor 3.0 global index?"):
            run(["migrate"])

    def purge():
        if warn_cursor_running("purge") is False:
            return
        if confirm_action("Purge", "Delete chats from Cursor DB to free space?"):
            run(["purge"])

    def delete_empty_chats():
        if warn_cursor_running("purge") is False:
            return
        all_chats = list_all_chats_with_sizes()
        empty_ids = [c["composerId"] for c in all_chats if c["messageCount"] == 0]
        if not empty_ids:
            log_append("No empty (0-message) chats found.\n")
            return
        if not confirm_action(
            "Delete empty chats",
            f"Delete {len(empty_ids)} empty (0-message) chat(s) across all workspaces?",
        ):
            return

        def _purge():
            deleted, keys_removed = purge_chats(empty_ids, force=True)
            print(f"Deleted {deleted} empty chat(s), removed {keys_removed:,} DB keys.")

        runner.run_callable(_purge)

    maint_buttons = [
        ("Doctor", lambda: run(["doctor"])),
        ("Doctor recover", doctor_recover),
        ("Repair blobs", lambda: run(["repair"])),
        ("Migrate", migrate),
        ("Migrate dry-run", lambda: run(["migrate", "--dry-run"])),
        ("Purge", purge),
        ("Delete empty chats", delete_empty_chats),
    ]
    row = ctk.CTkFrame(maint_frame, fg_color="transparent")
    row.pack(fill="x")
    for i, (label, cmd) in enumerate(maint_buttons):
        if i > 0 and i % 3 == 0:
            row = ctk.CTkFrame(maint_frame, fg_color="transparent")
            row.pack(fill="x")
        ctk.CTkButton(row, text=label, command=cmd, width=130).pack(side="left", padx=4, pady=4)

    ctk.CTkLabel(frame, text="Delete snapshots", font=ctk.CTkFont(weight="bold")).pack(
        anchor="w", padx=4, pady=(12, 4),
    )
    del_frame = ctk.CTkFrame(frame, fg_color="transparent")
    del_frame.pack(fill="x", pady=4)
    del_id = ctk.CTkEntry(del_frame, placeholder_text="Snapshot / composer ID", width=280)
    del_id.pack(side="left", padx=4)
    ws_del = WorkspaceSelector(del_frame, label="")

    def delete_id():
        sid = del_id.get().strip()
        if not sid:
            return
        if confirm_action("Delete", f"Delete snapshot {sid}?"):
            w = ws_del.get_workspace_arg()
            args = ["delete", "--id", sid, "-y"]
            if w:
                args.extend(["-w", w])
            run(args)

    def delete_all_project():
        if confirm_action("Delete all", "Delete all snapshots for this workspace project?"):
            w = ws_del.get_workspace_arg()
            args = ["delete", "--all", "-y"]
            if w:
                args.extend(["-w", w])
            run(args)

    def delete_all_projects():
        if not confirm_action("Delete ALL", "Delete ALL snapshots for ALL projects?"):
            return
        if confirm_action("Confirm", "This cannot be undone. Really delete everything?"):
            run(["delete", "--all-projects", "-y"])

    ctk.CTkButton(del_frame, text="Delete by ID", command=delete_id, width=120).pack(
        side="left", padx=4,
    )
    ctk.CTkButton(del_frame, text="Delete all (project)", command=delete_all_project, width=150).pack(
        side="left", padx=4,
    )
    ctk.CTkButton(del_frame, text="Delete ALL projects", command=delete_all_projects, width=150).pack(
        side="left", padx=4,
    )

    ctk.CTkLabel(frame, text="Copy chats between workspaces", font=ctk.CTkFont(weight="bold")).pack(
        anchor="w", padx=4, pady=(12, 4),
    )
    copy_frame = ctk.CTkFrame(frame, fg_color="transparent")
    copy_frame.pack(fill="x", pady=4)

    src_ws = WorkspaceSelector(copy_frame, label="Source workspace")
    tgt_ws = WorkspaceSelector(copy_frame, label="Target workspace")
    chat_list = ChatCheckList(copy_frame)
    force_var = ctk.BooleanVar(value=False)
    ctk.CTkCheckBox(copy_frame, text="Force", variable=force_var).pack(anchor="w", padx=4, pady=4)

    def load_chats():
        ws = src_ws.get_workspace()
        if ws:
            chat_list.load(ws["path"], ws["workspace_dir"])

    ctk.CTkButton(copy_frame, text="Load chats from source", command=load_chats, width=180).pack(
        anchor="w", padx=4, pady=4,
    )

    def do_copy():
        source = src_ws.get_workspace()
        target = tgt_ws.get_workspace()
        if not source or not target:
            log_append("Select source and target workspaces.\n")
            return
        if str(source["workspace_dir"]) == str(target["workspace_dir"]):
            log_append("Source and target must be different.\n")
            return
        ids = chat_list.selected_ids()
        if not ids:
            log_append("No chats selected.\n")
            return

        def _copy():
            success, failure = copy_between_workspaces(
                ids,
                source["workspace_dir"],
                target["workspace_dir"],
                source_path=source["path"],
                target_path=target["path"],
                force=force_var.get(),
            )
            print(f"Copy done: {success} succeeded, {failure} failed.")

        runner.run_callable(_copy)

    ctk.CTkButton(copy_frame, text="Copy selected chats", command=do_copy, width=180).pack(
        anchor="w", padx=4, pady=8,
    )

    ctk.CTkLabel(
        frame, text="Move chats to another workspace", font=ctk.CTkFont(weight="bold"),
    ).pack(anchor="w", padx=4, pady=(12, 4))
    ctk.CTkLabel(
        frame,
        text="Re-tags chats to the target workspace (rewrites workspaceIdentifier). "
        "The chat and its history move; nothing is duplicated.",
        text_color="gray",
        wraplength=500,
        justify="left",
    ).pack(anchor="w", padx=4, pady=(0, 4))
    move_frame = ctk.CTkFrame(frame, fg_color="transparent")
    move_frame.pack(fill="x", pady=4)

    mv_src_ws = WorkspaceSelector(move_frame, label="Source workspace (to load chats)")
    mv_tgt_ws = WorkspaceSelector(move_frame, label="Target workspace")
    mv_chat_list = ChatCheckList(move_frame)
    mv_force_var = ctk.BooleanVar(value=False)
    ctk.CTkCheckBox(move_frame, text="Force", variable=mv_force_var).pack(
        anchor="w", padx=4, pady=4,
    )

    def load_move_chats():
        ws = mv_src_ws.get_workspace()
        if ws:
            mv_chat_list.load(ws["path"], ws["workspace_dir"])

    ctk.CTkButton(
        move_frame, text="Load chats from source", command=load_move_chats, width=180,
    ).pack(anchor="w", padx=4, pady=4)

    def do_move():
        target = mv_tgt_ws.get_workspace()
        if not target:
            log_append("Select a target workspace.\n")
            return
        ids = mv_chat_list.selected_ids()
        if not ids:
            log_append("No chats selected.\n")
            return
        source = mv_src_ws.get_workspace()
        if source and str(source["workspace_dir"]) == str(target["workspace_dir"]):
            log_append("Source and target must be different.\n")
            return
        if warn_cursor_running("move-chat", allow_force=True) is False:
            return
        if not confirm_action(
            "Move chats", f"Re-tag {len(ids)} chat(s) to {target['path']}?",
        ):
            return

        to_ws_id = target["workspace_dir"].name

        def _move():
            moved, skipped = move_chat(ids, to_ws_id, force=mv_force_var.get())
            print(f"Move done: {moved} moved, {skipped} skipped.")

        runner.run_callable(_move)

    ctk.CTkButton(
        move_frame, text="Move selected chats", command=do_move, width=180,
    ).pack(anchor="w", padx=4, pady=8)

    ctk.CTkLabel(frame, text="Manage synced chats", font=ctk.CTkFont(weight="bold")).pack(
        anchor="w", padx=4, pady=(12, 4),
    )
    ctk.CTkLabel(
        frame,
        text="Remove stops sync forever. Pin keeps chats during retention pruning.",
        text_color="gray",
        wraplength=500,
        justify="left",
    ).pack(anchor="w", padx=4, pady=(0, 4))

    manage_frame = ctk.CTkFrame(frame, fg_color="transparent")
    manage_frame.pack(fill="x", pady=4)
    ws_manage = WorkspaceSelector(manage_frame, label="Workspace")
    manage_list = ChatCheckList(manage_frame, height=140)

    def load_manage_chats():
        ws = ws_manage.get_workspace()
        if ws:
            manage_list.load(ws["path"], ws["workspace_dir"])

    ctk.CTkButton(
        manage_frame,
        text="Load chats",
        command=load_manage_chats,
        width=120,
    ).pack(anchor="w", padx=4, pady=4)

    manage_btn_row = ctk.CTkFrame(manage_frame, fg_color="transparent")
    manage_btn_row.pack(fill="x", padx=4, pady=4)

    def remove_selected():
        if warn_cursor_running("remove", allow_force=True) is False:
            return
        ids = manage_list.selected_ids()
        if not ids:
            log_append("No chats selected.\n")
            return
        if not confirm_action(
            "Remove chats",
            f"Remove {len(ids)} chat(s) from sync and Cursor?\nThey will never sync again.",
        ):
            return
        args = ["remove", "--ids", ",".join(ids), "-y", "--force"]
        run(args)

    def pin_selected():
        ids = manage_list.selected_ids()
        if not ids:
            log_append("No chats selected.\n")
            return
        for cid in ids:
            run(["pin", "--id", cid])

    def unpin_selected():
        ids = manage_list.selected_ids()
        if not ids:
            log_append("No chats selected.\n")
            return
        for cid in ids:
            run(["pin", "--id", cid, "--unpin"])

    ctk.CTkButton(manage_btn_row, text="Remove selected", command=remove_selected, width=130).pack(
        side="left", padx=4,
    )
    ctk.CTkButton(manage_btn_row, text="Pin / Favorito", command=pin_selected, width=120).pack(
        side="left", padx=4,
    )
    ctk.CTkButton(manage_btn_row, text="Unpin", command=unpin_selected, width=80).pack(
        side="left", padx=4,
    )
