import json
import os
import re
import threading
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from .db import open_db
from .job_ops import delete_job
from .mapper import analyze_dvd_menu, map_job_episodes, set_mapping_override, set_mapping_source_override
from .pipeline import run_pipeline_for_job
from .rip import discover_optical_drives
from .splitter import set_manual_split_timestamps
from .state import create_job, get_job, list_jobs, update_job_disc_profile
from .tmdb import identify_job_with_tmdb, select_tmdb_candidate

STATUS_ORDER = [
    "queued",
    "ripping",
    "identifying",
    "mapping",
    "splitting",
    "renaming",
    "copying",
    "done",
]


def launch_gui(conn, cfg, refresh_seconds: int = 3) -> None:
    root = tk.Tk()
    root.title("Auto-Ripper")
    root.geometry("1280x820")
    root.minsize(1150, 740)
    root.configure(bg="#F3F6FB")

    style = ttk.Style(root)
    theme = "clam" if "clam" in style.theme_names() else ("vista" if "vista" in style.theme_names() else style.theme_use())
    style.theme_use(theme)
    style.configure(".", background="#F3F6FB", foreground="#1F2937", font=("Segoe UI", 10))
    style.configure("TFrame", background="#F3F6FB")
    style.configure("TLabelframe", background="#FFFFFF", bordercolor="#D8E0EC", relief="solid")
    style.configure("TLabelframe.Label", background="#F3F6FB", foreground="#1F2937")
    style.configure("TLabel", background="#F3F6FB", foreground="#1F2937")
    style.configure("TEntry", fieldbackground="#FFFFFF", bordercolor="#CBD5E1", padding=6)
    style.configure("TCombobox", fieldbackground="#FFFFFF", bordercolor="#CBD5E1", padding=4)
    style.configure("TRadiobutton", background="#F3F6FB", foreground="#1F2937")
    style.configure("Treeview", background="#FFFFFF", fieldbackground="#FFFFFF", foreground="#1F2937", rowheight=26, bordercolor="#D8E0EC")
    style.configure("Treeview.Heading", background="#EAF0F8", foreground="#334155", font=("Segoe UI", 9, "bold"), relief="flat")
    style.map("Treeview", background=[("selected", "#DCEBFF")], foreground=[("selected", "#0F172A")])
    style.configure("TNotebook", background="#F3F6FB", borderwidth=0)
    style.configure("TNotebook.Tab", padding=(14, 8), background="#E9EEF6", foreground="#475569")
    style.map("TNotebook.Tab", background=[("selected", "#FFFFFF")], foreground=[("selected", "#0F172A")])
    style.configure("TButton", padding=(10, 7), background="#FFFFFF", foreground="#1F2937", bordercolor="#CBD5E1", relief="flat")
    style.map("TButton", background=[("active", "#EEF2F7")])
    style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
    style.configure("Subtle.TLabel", foreground="#64748B")
    style.configure("Primary.TButton", padding=(12, 8), background="#2563EB", foreground="#FFFFFF", bordercolor="#2563EB")
    style.map("Primary.TButton", background=[("active", "#1D4ED8")], foreground=[("active", "#FFFFFF")])
    style.configure("Secondary.TButton", padding=(10, 7), background="#EAF0F8", foreground="#1E3A5F", bordercolor="#C7D5E6")
    style.map("Secondary.TButton", background=[("active", "#DCE8F7")])
    style.configure("Stage.TLabel", font=("Segoe UI", 9, "bold"))
    style.configure("Section.TLabelframe", padding=10)
    style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
    style.configure("MetricLabel.TLabel", font=("Segoe UI", 9, "bold"), foreground="#4A4A4A")
    style.configure("MetricValue.TLabel", font=("Segoe UI", 10))

    root_frame = ttk.Frame(root, padding=16)
    root_frame.pack(fill=tk.BOTH, expand=True)

    header = ttk.Frame(root_frame)
    header.pack(fill=tk.X)
    ttk.Label(header, text="DVD Auto-Ripper", style="Title.TLabel").pack(side=tk.LEFT)
    ttk.Label(
        header,
        text="One-click rip, identify, map, split, finalize, transfer",
        style="Subtle.TLabel",
    ).pack(side=tk.LEFT, padx=(12, 0), pady=(5, 0))

    # --- Progress strip ---
    progress_wrap = ttk.LabelFrame(root_frame, text="Pipeline Progress", padding=10, style="Section.TLabelframe")
    progress_wrap.pack(fill=tk.X, pady=(8, 0))
    stage_labels: dict[str, tk.Label] = {}
    for idx, stage in enumerate(STATUS_ORDER):
        lbl = tk.Label(
            progress_wrap,
            text=stage.upper(),
            bg="#E5EAF2",
            fg="#334155",
            padx=10,
            pady=5,
            relief="flat",
            font=("Segoe UI", 9, "bold"),
        )
        lbl.pack(side=tk.LEFT)
        stage_labels[stage] = lbl
        if idx < len(STATUS_ORDER) - 1:
            ttk.Label(progress_wrap, text="→").pack(side=tk.LEFT, padx=4)

    # --- Start bar ---
    start_bar = ttk.LabelFrame(root_frame, text="Start New Disc", padding=12, style="Section.TLabelframe")
    start_bar.pack(fill=tk.X, pady=(10, 8))

    ttk.Label(start_bar, text="Detected Disc Label:").grid(row=0, column=0, sticky="w")
    disc_label_var = tk.StringVar(value=_pick_disc_label())
    disc_label_entry = ttk.Entry(start_bar, textvariable=disc_label_var, width=40)
    disc_label_entry.grid(row=0, column=1, padx=(8, 12), sticky="w")
    ttk.Button(start_bar, text="Refresh Label", command=lambda: disc_label_var.set(_pick_disc_label())).grid(
        row=0, column=2, padx=(0, 14), sticky="w"
    )

    ttk.Label(start_bar, text="Media Type:").grid(row=0, column=3, sticky="w")
    media_var = tk.StringVar(value="tv")
    media_frame = ttk.Frame(start_bar)
    media_frame.grid(row=0, column=4, padx=(8, 0), sticky="w")
    ttk.Radiobutton(media_frame, text="TV Show", variable=media_var, value="tv").pack(side=tk.LEFT, padx=(0, 10))
    ttk.Radiobutton(media_frame, text="Movie", variable=media_var, value="movie").pack(side=tk.LEFT)

    ttk.Label(start_bar, text="Disc Scope:").grid(row=1, column=0, sticky="w", pady=(8, 0))
    disc_scope_var = tk.StringVar(value="full_season")
    disc_scope_combo = ttk.Combobox(
        start_bar,
        textvariable=disc_scope_var,
        values=("full_season", "partial_season", "special"),
        state="readonly",
        width=18,
    )
    disc_scope_combo.grid(row=1, column=1, padx=(8, 12), sticky="w", pady=(8, 0))

    ttk.Label(start_bar, text="Season #:").grid(row=1, column=2, sticky="w", pady=(8, 0))
    season_var = tk.StringVar(value="1")
    ttk.Entry(start_bar, textvariable=season_var, width=6).grid(row=1, column=3, padx=(8, 12), sticky="w", pady=(8, 0))

    ttk.Label(start_bar, text="Episode Range:").grid(row=1, column=4, sticky="w", pady=(8, 0))
    range_frame = ttk.Frame(start_bar)
    range_frame.grid(row=1, column=5, sticky="w", pady=(8, 0))
    range_start_var = tk.StringVar()
    range_end_var = tk.StringVar()
    ttk.Entry(range_frame, textvariable=range_start_var, width=6).pack(side=tk.LEFT)
    ttk.Label(range_frame, text="to").pack(side=tk.LEFT, padx=4)
    ttk.Entry(range_frame, textvariable=range_end_var, width=6).pack(side=tk.LEFT)

    # --- Main split ---
    main = ttk.Panedwindow(root_frame, orient=tk.HORIZONTAL)
    main.pack(fill=tk.BOTH, expand=True)

    left = ttk.Frame(main, padding=(0, 8, 8, 0))
    right = ttk.Frame(main, padding=(8, 8, 0, 0))
    main.add(left, weight=5)
    main.add(right, weight=7)

    # --- Left: jobs table + actions ---
    jobs_wrap = ttk.LabelFrame(left, text="Jobs", padding=8)
    jobs_wrap.pack(fill=tk.BOTH, expand=True)

    jobs_tree = ttk.Treeview(
        jobs_wrap,
        columns=("status", "media_type", "label", "updated"),
        show="headings",
        height=17,
    )
    columns = (
        ("status", "Status", 120),
        ("media_type", "Type", 90),
        ("label", "Disc Label", 280),
        ("updated", "Updated", 220),
    )
    for col, title, width in columns:
        jobs_tree.heading(col, text=title)
        jobs_tree.column(col, width=width, anchor="w")
    jobs_tree.tag_configure("status_error", background="#FDECEC")
    jobs_tree.tag_configure("status_done", background="#EAF7EA")
    jobs_tree.tag_configure("status_active", background="#EAF2FE")

    jobs_scroll = ttk.Scrollbar(jobs_wrap, orient=tk.VERTICAL, command=jobs_tree.yview)
    jobs_tree.configure(yscrollcommand=jobs_scroll.set)
    jobs_tree.grid(row=0, column=0, sticky="nsew")
    jobs_scroll.grid(row=0, column=1, sticky="ns")
    jobs_wrap.rowconfigure(0, weight=1)
    jobs_wrap.columnconfigure(0, weight=1)

    left_actions = ttk.Frame(left)
    left_actions.pack(fill=tk.X, pady=(8, 0))
    btn_start = ttk.Button(left_actions, text="Start End-to-End", style="Primary.TButton")
    btn_resume = ttk.Button(left_actions, text="Resume Selected", style="Secondary.TButton")
    btn_delete = ttk.Button(left_actions, text="Delete Selected")
    btn_refresh = ttk.Button(left_actions, text="Refresh Jobs")
    btn_start.pack(side=tk.LEFT, padx=(0, 6))
    btn_resume.pack(side=tk.LEFT, padx=(0, 6))
    btn_delete.pack(side=tk.LEFT, padx=(0, 6))
    btn_refresh.pack(side=tk.LEFT)

    # --- Right: details + guided tools ---
    guide_wrap = ttk.LabelFrame(right, text="Actions", style="Section.TLabelframe")
    guide_wrap.pack(fill=tk.X, pady=(0, 8))
    btn_identify = ttk.Button(guide_wrap, text="Re-run TMDB Identify", style="Secondary.TButton")
    btn_map = ttk.Button(guide_wrap, text="Re-run Mapping", style="Primary.TButton")
    btn_menu = ttk.Button(guide_wrap, text="Analyze DVD Menu", style="Secondary.TButton")
    btn_select_tmdb = ttk.Button(guide_wrap, text="Override TMDB")
    btn_map_override = ttk.Button(guide_wrap, text="Override Mapping")
    btn_file_override = ttk.Button(guide_wrap, text="Override File")
    btn_split_override = ttk.Button(guide_wrap, text="Override Split")
    for idx, btn in enumerate(
        (btn_identify, btn_map, btn_menu, btn_select_tmdb, btn_map_override, btn_file_override, btn_split_override)
    ):
        btn.grid(row=idx // 4, column=idx % 4, padx=(0 if idx % 4 == 0 else 6, 0), pady=(0, 6), sticky="ew")
    for col in range(4):
        guide_wrap.columnconfigure(col, weight=1)

    rip_live_wrap = ttk.LabelFrame(right, text="Live Rip Monitor", padding=10, style="Section.TLabelframe")
    rip_live_wrap.pack(fill=tk.X, pady=(0, 8))
    rip_live_var = tk.StringVar(value="No active rip selected.")
    ttk.Label(rip_live_wrap, textvariable=rip_live_var, style="Subtle.TLabel").pack(anchor="w")

    notebook = ttk.Notebook(right)
    notebook.pack(fill=tk.BOTH, expand=True)

    overview_tab = ttk.Frame(notebook, padding=8)
    json_tab = ttk.Frame(notebook, padding=8)
    artifacts_tab = ttk.Frame(notebook, padding=8)
    notebook.add(overview_tab, text="Overview")
    notebook.add(json_tab, text="Raw JSON")
    notebook.add(artifacts_tab, text="Artifacts")

    summary_wrap = ttk.LabelFrame(overview_tab, text="Job Summary", style="Section.TLabelframe")
    summary_wrap.pack(fill=tk.X, pady=(0, 8))
    summary_wrap.columnconfigure(1, weight=1)
    summary_wrap.columnconfigure(3, weight=1)
    job_status_var = tk.StringVar(value="-")
    job_disc_var = tk.StringVar(value="-")
    job_scope_var = tk.StringVar(value="-")
    job_tmdb_var = tk.StringVar(value="-")
    job_outputs_var = tk.StringVar(value="-")
    summary_fields = (
        ("Status", job_status_var, 0, 0),
        ("Disc", job_disc_var, 0, 2),
        ("Scope", job_scope_var, 1, 0),
        ("TMDB", job_tmdb_var, 1, 2),
        ("Outputs", job_outputs_var, 2, 0),
    )
    for label, var, row, col in summary_fields:
        ttk.Label(summary_wrap, text=f"{label}:", style="MetricLabel.TLabel").grid(row=row, column=col, sticky="w", padx=(0, 8), pady=4)
        ttk.Label(summary_wrap, textvariable=var, style="MetricValue.TLabel").grid(row=row, column=col + 1, sticky="ew", pady=4)

    mappings_wrap = ttk.LabelFrame(overview_tab, text="Episode Bundles", style="Section.TLabelframe")
    mappings_wrap.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
    mappings_tree = ttk.Treeview(
        mappings_wrap,
        columns=("file", "episodes", "titles", "confidence"),
        show="headings",
        height=8,
    )
    mapping_cols = (
        ("file", "Source File", 180),
        ("episodes", "Episodes", 90),
        ("titles", "Recovered Titles", 420),
        ("confidence", "Confidence", 90),
    )
    for col, title, width in mapping_cols:
        mappings_tree.heading(col, text=title)
        mappings_tree.column(col, width=width, anchor="w")
    mappings_scroll = ttk.Scrollbar(mappings_wrap, orient=tk.VERTICAL, command=mappings_tree.yview)
    mappings_tree.configure(yscrollcommand=mappings_scroll.set)
    mappings_tree.grid(row=0, column=0, sticky="nsew")
    mappings_scroll.grid(row=0, column=1, sticky="ns")
    mappings_wrap.rowconfigure(0, weight=1)
    mappings_wrap.columnconfigure(0, weight=1)

    outputs_wrap = ttk.LabelFrame(overview_tab, text="Final Outputs", style="Section.TLabelframe")
    outputs_wrap.pack(fill=tk.BOTH, expand=True)
    outputs_tree = ttk.Treeview(
        outputs_wrap,
        columns=("episode", "file", "transfer"),
        show="headings",
        height=8,
    )
    output_cols = (
        ("episode", "Episode", 90),
        ("file", "Final File", 520),
        ("transfer", "Transfer", 100),
    )
    for col, title, width in output_cols:
        outputs_tree.heading(col, text=title)
        outputs_tree.column(col, width=width, anchor="w")
    outputs_scroll = ttk.Scrollbar(outputs_wrap, orient=tk.VERTICAL, command=outputs_tree.yview)
    outputs_tree.configure(yscrollcommand=outputs_scroll.set)
    outputs_tree.grid(row=0, column=0, sticky="nsew")
    outputs_scroll.grid(row=0, column=1, sticky="ns")
    outputs_wrap.rowconfigure(0, weight=1)
    outputs_wrap.columnconfigure(0, weight=1)

    details_wrap = ttk.LabelFrame(json_tab, text="Raw Job Data", style="Section.TLabelframe")
    details_wrap.pack(fill=tk.BOTH, expand=True)
    details = tk.Text(
        details_wrap,
        height=24,
        wrap="none",
        font=("Consolas", 10),
        padx=6,
        pady=6,
    )
    details_v = ttk.Scrollbar(details_wrap, orient=tk.VERTICAL, command=details.yview)
    details_h = ttk.Scrollbar(details_wrap, orient=tk.HORIZONTAL, command=details.xview)
    details.configure(yscrollcommand=details_v.set, xscrollcommand=details_h.set)
    details.grid(row=0, column=0, sticky="nsew")
    details_v.grid(row=0, column=1, sticky="ns")
    details_h.grid(row=1, column=0, sticky="ew")
    details_wrap.rowconfigure(0, weight=1)
    details_wrap.columnconfigure(0, weight=1)

    artifacts_wrap = ttk.LabelFrame(artifacts_tab, text="Analysis Artifacts", style="Section.TLabelframe")
    artifacts_wrap.pack(fill=tk.BOTH, expand=True)
    artifacts_tree = ttk.Treeview(
        artifacts_wrap,
        columns=("kind", "path"),
        show="headings",
        height=14,
    )
    artifacts_tree.heading("kind", text="Kind")
    artifacts_tree.heading("path", text="Path")
    artifacts_tree.column("kind", width=160, anchor="w")
    artifacts_tree.column("path", width=620, anchor="w")
    artifacts_scroll = ttk.Scrollbar(artifacts_wrap, orient=tk.VERTICAL, command=artifacts_tree.yview)
    artifacts_tree.configure(yscrollcommand=artifacts_scroll.set)
    artifacts_tree.grid(row=0, column=0, sticky="nsew")
    artifacts_scroll.grid(row=0, column=1, sticky="ns")
    artifacts_wrap.rowconfigure(0, weight=1)
    artifacts_wrap.columnconfigure(0, weight=1)
    artifacts_actions = ttk.Frame(artifacts_wrap)
    artifacts_actions.grid(row=1, column=0, sticky="ew", pady=(8, 0))
    btn_open_artifact = ttk.Button(artifacts_actions, text="Open Selected Artifact")
    btn_copy_artifact = ttk.Button(artifacts_actions, text="Copy Path")
    btn_open_artifact.pack(side=tk.LEFT)
    btn_copy_artifact.pack(side=tk.LEFT, padx=(6, 0))

    status_bar = ttk.Frame(root_frame)
    status_bar.pack(fill=tk.X, pady=(8, 0))
    status_var = tk.StringVar(value="Idle")
    ttk.Label(status_bar, text="Status:").pack(side=tk.LEFT)
    ttk.Label(status_bar, textvariable=status_var).pack(side=tk.LEFT, padx=(6, 0))
    busy_state = {"value": False, "active_job_id": None, "active_action": None}

    selected_job_id = {"value": None}
    last_details_cache = {"job_id": None, "payload": None}

    def _set_status(text: str) -> None:
        status_var.set(text)

    def _update_rip_live_label_local(rip_monitor: dict) -> None:
        if not rip_monitor:
            rip_live_var.set("No rip monitor data.")
            return
        if rip_monitor.get("message"):
            rip_live_var.set(str(rip_monitor["message"]))
            return
        file_count = rip_monitor.get("file_count", 0)
        size_mb = rip_monitor.get("size_mb", 0)
        stale = rip_monitor.get("stale_seconds")
        tail = rip_monitor.get("log_tail") or ""
        status_text = f"Files: {file_count} | Size: {size_mb} MB"
        if stale is not None:
            if stale >= 180:
                status_text += f" | WARNING: no growth for {stale}s"
            else:
                status_text += f" | Last change: {stale}s ago"
        if tail:
            status_text += f" | Tail: {tail.replace(chr(10), ' | ')}"
        rip_live_var.set(status_text)

    def _set_busy(is_busy: bool) -> None:
        busy_state["value"] = is_busy
        if not is_busy:
            busy_state["active_job_id"] = None
            busy_state["active_action"] = None
        _update_button_states()

    def _update_button_states() -> None:
        sid = selected_id()
        has_selection = sid is not None
        is_busy = bool(busy_state["value"])
        running_job_id = busy_state["active_job_id"]

        btn_start.configure(state=tk.DISABLED if is_busy else tk.NORMAL)
        btn_resume.configure(state=tk.NORMAL if has_selection else tk.DISABLED)
        # Keep delete available while another job runs; block only for the active running job.
        can_delete = has_selection and not (is_busy and sid == running_job_id)
        btn_delete.configure(state=tk.NORMAL if can_delete else tk.DISABLED)
        btn_refresh.configure(state=tk.NORMAL)
        btn_identify.configure(state=tk.DISABLED if is_busy or not has_selection else tk.NORMAL)
        btn_map.configure(state=tk.DISABLED if is_busy or not has_selection else tk.NORMAL)
        btn_menu.configure(state=tk.DISABLED if is_busy or not has_selection else tk.NORMAL)
        btn_select_tmdb.configure(state=tk.DISABLED if is_busy or not has_selection else tk.NORMAL)
        btn_map_override.configure(state=tk.DISABLED if is_busy or not has_selection else tk.NORMAL)
        btn_file_override.configure(state=tk.DISABLED if is_busy or not has_selection else tk.NORMAL)
        btn_split_override.configure(state=tk.DISABLED if is_busy or not has_selection else tk.NORMAL)

    def _update_progress_strip(status: str | None) -> None:
        current = status or ""
        for stage in STATUS_ORDER:
            stage_labels[stage].configure(bg="#E6E6E6", fg="#2B2B2B")
        if current == "error":
            for stage in STATUS_ORDER:
                stage_labels[stage].configure(bg="#E6E6E6", fg="#2B2B2B")
            return
        if current not in STATUS_ORDER:
            return
        current_idx = STATUS_ORDER.index(current)
        for idx, stage in enumerate(STATUS_ORDER):
            if idx < current_idx:
                stage_labels[stage].configure(bg="#CFE9CF", fg="#1D4E1D")
            elif idx == current_idx:
                stage_labels[stage].configure(bg="#BDD8FF", fg="#123A6D")

    def _run_background(name: str, fn, job_id: str | None = None):
        if busy_state["value"]:
            messagebox.showinfo(
                name,
                f"{busy_state.get('active_action') or 'Another operation'} is already running.",
            )
            return

        def worker():
            worker_conn = None
            try:
                root.after(
                    0,
                    lambda: (
                        busy_state.__setitem__("active_job_id", job_id),
                        busy_state.__setitem__("active_action", name),
                        _set_busy(True),
                        _set_status(f"{name}..."),
                    ),
                )
                worker_conn = open_db(cfg.db_path)
                result = fn(worker_conn)
                root.after(0, lambda: (_set_busy(False), _set_status("Idle")))
                if result is not None:
                    root.after(0, lambda: _show_guidance(result))
            except Exception as exc:
                err_msg = str(exc)
                root.after(0, lambda: (_set_busy(False), _set_status("Idle")))
                root.after(0, lambda err=err_msg: messagebox.showerror(name, err))
            finally:
                try:
                    if worker_conn is not None:
                        worker_conn.close()
                except Exception:
                    pass
                def _refresh_and_show_selected():
                    refresh_jobs()
                    sid = selected_id()
                    if sid:
                        show_job(sid)

                root.after(0, _refresh_and_show_selected)

        threading.Thread(target=worker, daemon=True).start()

    def refresh_jobs() -> None:
        jobs_tree.delete(*jobs_tree.get_children())
        for job in list_jobs(conn):
            tags = ()
            if job["status"] == "error":
                tags = ("status_error",)
            elif job["status"] == "done":
                tags = ("status_done",)
            elif job["status"] in STATUS_ORDER:
                tags = ("status_active",)
            jobs_tree.insert(
                "",
                tk.END,
                iid=job["id"],
                values=(job["status"], job["media_type"], job["disc_label"], job["updated_at"]),
                tags=tags,
            )
        if selected_job_id["value"] and selected_job_id["value"] in jobs_tree.get_children():
            jobs_tree.selection_set(selected_job_id["value"])
            show_job(selected_job_id["value"])
        elif jobs_tree.get_children():
            first = jobs_tree.get_children()[0]
            jobs_tree.selection_set(first)
            show_job(first)
        else:
            mappings_tree.delete(*mappings_tree.get_children())
            outputs_tree.delete(*outputs_tree.get_children())
            artifacts_tree.delete(*artifacts_tree.get_children())
            details.delete("1.0", tk.END)
            details.insert("1.0", "No jobs yet. Insert a disc and click Start End-to-End.")
            job_status_var.set("-")
            job_disc_var.set("-")
            job_scope_var.set("-")
            job_tmdb_var.set("-")
            job_outputs_var.set("-")
            _update_progress_strip(None)
        _update_button_states()

    def selected_id() -> str | None:
        sel = jobs_tree.selection()
        return sel[0] if sel else None

    def show_job(job_id: str) -> None:
        selected_job_id["value"] = job_id
        job = get_job(conn, job_id)
        if not job:
            return
        tmdb = conn.execute(
            """
            SELECT tmdb_id, media_type, title, year, score, selected, manual_override
            FROM tmdb_candidates
            WHERE job_id = ?
            ORDER BY score DESC
            """,
            (job_id,),
        ).fetchall()
        mappings = conn.execute(
            """
            SELECT
                em.id,
                em.rip_title_id,
                rt.title_id,
                rt.source_file,
                em.episode_start,
                em.episode_end,
                em.tmdb_episode_ids_json,
                em.episode_titles_json,
                em.confidence,
                em.reason,
                em.manual_override,
                em.needs_split
            FROM episode_mappings em
            LEFT JOIN rip_titles rt ON rt.id = em.rip_title_id
            WHERE em.job_id = ?
            ORDER BY em.id
            """,
            (job_id,),
        ).fetchall()
        splits = conn.execute(
            """
            SELECT id, mapping_id, segment_index, start_seconds, end_seconds, status, error_message
            FROM split_plans
            WHERE job_id = ?
            ORDER BY id
            """,
            (job_id,),
        ).fetchall()
        outputs = conn.execute(
            """
            SELECT local_path, nas_path, transfer_status
            FROM outputs
            WHERE job_id = ?
            ORDER BY id
            """,
            (job_id,),
        ).fetchall()
        selected_media = conn.execute(
            """
            SELECT media_type, tmdb_id, title, year, season_number
            FROM job_selected_media
            WHERE job_id = ?
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        menu_analysis = _load_job_artifact_json(cfg.staging_root, job_id, "menu_analysis", "menu_analysis.json")
        bundle_association = _load_job_artifact_json(cfg.staging_root, job_id, "menu_analysis", "bundle_association.json")
        payload = {
            "job": dict(job),
            "selected_media": dict(selected_media) if selected_media else None,
            "tmdb_candidates": [dict(r) for r in tmdb],
            "episode_mappings": [dict(r) for r in mappings],
            "split_plans": [dict(r) for r in splits],
            "outputs": [dict(r) for r in outputs],
            "menu_analysis": menu_analysis,
            "bundle_association": bundle_association,
            "rip_monitor": _build_rip_monitor(cfg.staging_root, job_id, str(job["status"])),
        }
        rendered = json.dumps(payload, indent=2)
        yview = details.yview()
        xview = details.xview()
        should_rerender = (
            last_details_cache["job_id"] != job_id
            or last_details_cache["payload"] != rendered
        )
        if should_rerender:
            details.delete("1.0", tk.END)
            details.insert("1.0", rendered)
            if last_details_cache["job_id"] == job_id:
                details.yview_moveto(yview[0] if yview else 0.0)
                details.xview_moveto(xview[0] if xview else 0.0)
            last_details_cache["job_id"] = job_id
            last_details_cache["payload"] = rendered
        _populate_overview(payload)
        _populate_artifacts(payload)
        _update_progress_strip(str(job["status"]))
        _update_rip_live_label_local(payload["rip_monitor"])

    def _populate_overview(payload: dict) -> None:
        job = payload.get("job") or {}
        selected_media = payload.get("selected_media") or {}
        mappings = payload.get("episode_mappings") or []
        outputs = payload.get("outputs") or []

        job_status_var.set(str(job.get("status") or "-"))
        job_disc_var.set(str(job.get("disc_label") or "-"))
        scope = str(job.get("disc_scope") or "unspecified")
        season = job.get("season_number")
        ep_start = job.get("episode_range_start")
        ep_end = job.get("episode_range_end")
        scope_text = scope
        if season:
            scope_text += f" | S{int(season):02d}"
        if ep_start is not None and ep_end is not None:
            scope_text += f" | E{int(ep_start)}-{int(ep_end)}"
        job_scope_var.set(scope_text)

        if selected_media:
            tmdb_text = str(selected_media.get("title") or "-")
            if selected_media.get("year"):
                tmdb_text += f" ({selected_media['year']})"
            if selected_media.get("season_number"):
                tmdb_text += f" S{int(selected_media['season_number']):02d}"
            job_tmdb_var.set(tmdb_text)
        else:
            job_tmdb_var.set("-")
        job_outputs_var.set(f"{len(outputs)} file(s)")

        mappings_tree.delete(*mappings_tree.get_children())
        for idx, mapping in enumerate(mappings, start=1):
            source_file = Path(str(mapping.get("source_file") or "")).name or "(none)"
            ep_start = mapping.get("episode_start")
            ep_end = mapping.get("episode_end")
            if ep_start is None:
                episode_label = "Excluded"
            elif ep_end is None or ep_end == ep_start:
                episode_label = f"E{int(ep_start):02d}"
            else:
                episode_label = f"E{int(ep_start):02d}-E{int(ep_end):02d}"
            titles = _display_episode_titles(mapping.get("episode_titles_json"))
            confidence = mapping.get("confidence")
            confidence_text = "-" if confidence is None else f"{float(confidence):.2f}"
            mappings_tree.insert(
                "",
                tk.END,
                iid=f"map-{idx}",
                values=(source_file, episode_label, titles, confidence_text),
            )

        outputs_tree.delete(*outputs_tree.get_children())
        for idx, output in enumerate(outputs, start=1):
            file_name = Path(str(output.get("local_path") or "")).name
            episode_match = re.search(r"s\d{2}e(\d{2})", file_name, re.IGNORECASE)
            ep_label = f"E{episode_match.group(1)}" if episode_match else "-"
            outputs_tree.insert(
                "",
                tk.END,
                iid=f"out-{idx}",
                values=(ep_label, file_name or "(none)", str(output.get("transfer_status") or "-")),
            )

    def _populate_artifacts(payload: dict) -> None:
        artifacts_tree.delete(*artifacts_tree.get_children())
        menu_analysis = payload.get("menu_analysis")
        bundle_association = payload.get("bundle_association")
        if menu_analysis:
            for key, value in menu_analysis.items():
                if isinstance(value, list):
                    for item in value:
                        artifacts_tree.insert("", tk.END, values=(key, str(item)))
                else:
                    artifacts_tree.insert("", tk.END, values=(key, str(value)))
        if bundle_association:
            for key, value in bundle_association.items():
                artifacts_tree.insert("", tk.END, values=(f"bundle_{key}", str(value)))
        if not artifacts_tree.get_children():
            artifacts_tree.insert("", tk.END, values=("info", "No analysis artifacts cached for this job yet."))

    def on_select(_event=None):
        sid = selected_id()
        if sid:
            show_job(sid)
        _update_button_states()

    def _show_guidance(result: dict):
        if not isinstance(result, dict):
            return
        if result.get("needs_review"):
            msg = "Pipeline paused for input.\n\n"
            status = result.get("status")
            if status == "identifying":
                msg += "TMDB confidence is low. Click Override TMDB."
            elif status == "mapping":
                msg += "Mapping needs correction. Click Override Mapping."
            elif status == "splitting":
                msg += "Split failed/uncertain. Click Override Split with timestamps."
            elif status == "copying":
                msg += "NAS transfer had errors. Check NAS access and resume."
            else:
                msg += json.dumps(result, indent=2)
            messagebox.showwarning("Guidance Required", msg)
        elif result.get("status") == "done":
            messagebox.showinfo("Completed", "Job completed successfully.")
        elif result.get("status") == "deleted":
            messagebox.showinfo("Deleted", "Job and staged artifacts deleted.")

    def start_end_to_end():
        media_type = media_var.get().strip().lower()
        if media_type not in ("tv", "movie"):
            messagebox.showerror("Start End-to-End", "Choose TV Show or Movie.")
            return
        label = disc_label_var.get().strip() or _pick_disc_label()
        disc_scope = disc_scope_var.get().strip() or None
        season_number = None
        episode_range_start = None
        episode_range_end = None
        if media_type == "tv":
            season_text = season_var.get().strip()
            if season_text:
                try:
                    season_number = int(season_text)
                except ValueError:
                    messagebox.showerror("Start End-to-End", "Season number must be an integer.")
                    return
            if disc_scope == "partial_season":
                start_text = range_start_var.get().strip()
                end_text = range_end_var.get().strip()
                if not start_text or not end_text:
                    messagebox.showerror("Start End-to-End", "Partial season discs require an episode start and end.")
                    return
                try:
                    episode_range_start = int(start_text)
                    episode_range_end = int(end_text)
                except ValueError:
                    messagebox.showerror("Start End-to-End", "Episode range must be integers.")
                    return
                if episode_range_end < episode_range_start:
                    messagebox.showerror("Start End-to-End", "Episode range end must be >= start.")
                    return

        def task(worker_conn):
            job_id = create_job(
                worker_conn,
                disc_label=label,
                media_type=media_type,
                disc_scope=disc_scope,
                season_number=season_number,
                episode_range_start=episode_range_start,
                episode_range_end=episode_range_end,
            )
            selected_job_id["value"] = job_id
            return run_pipeline_for_job(worker_conn, cfg, job_id, mock_rip=False)

        _run_background("Start End-to-End", task)

    def resume_selected():
        job_id = selected_id()
        if not job_id:
            messagebox.showerror("Resume Selected", "Select a job first.")
            return

        def task(worker_conn):
            return run_pipeline_for_job(worker_conn, cfg, job_id, mock_rip=False)

        _run_background("Resume Selected", task, job_id=job_id)

    def delete_selected():
        job_id = selected_id()
        if not job_id:
            messagebox.showerror("Delete Selected", "Select a job first.")
            return
        if busy_state["value"] and job_id == busy_state.get("active_job_id"):
            messagebox.showerror("Delete Selected", "Cannot delete the job currently running.")
            return
        ok = messagebox.askyesno(
            "Delete Selected Job",
            "Delete this job, all DB records, and staged files?\n\nThis cannot be undone.",
            parent=root,
        )
        if not ok:
            return

        def task(worker_conn):
            result = delete_job(worker_conn, cfg.staging_root, job_id)
            selected_job_id["value"] = None
            return {"status": "deleted", "result": result}

        _run_background("Delete Selected", task, job_id=job_id)

    def run_identify():
        job_id = selected_id()
        if not job_id:
            return

        def task(worker_conn):
            return identify_job_with_tmdb(worker_conn, cfg, job_id)

        _run_background("TMDB Identify", task, job_id=job_id)

    def run_mapping():
        job_id = selected_id()
        if not job_id:
            return

        def task(worker_conn):
            return map_job_episodes(worker_conn, cfg, job_id)

        _run_background("Run Mapping", task, job_id=job_id)

    def run_menu_analysis():
        job_id = selected_id()
        if not job_id:
            return

        def task(worker_conn):
            return analyze_dvd_menu(worker_conn, cfg, job_id)

        _run_background("Analyze DVD Menu", task, job_id=job_id)

    def run_tmdb_override():
        job_id = selected_id()
        if not job_id:
            return
        candidate_rows = conn.execute(
            """
            SELECT tmdb_id, media_type, title, score
            FROM tmdb_candidates
            WHERE job_id = ?
            ORDER BY score DESC
            """,
            (job_id,),
        ).fetchall()
        if not candidate_rows:
            messagebox.showerror("Override TMDB", "No candidates found. Run TMDB Identify first.")
            return

        dialog = tk.Toplevel(root)
        dialog.title("Select TMDB Candidate")
        dialog.geometry("760x320")
        dialog.transient(root)
        dialog.grab_set()

        tree = ttk.Treeview(dialog, columns=("id", "media", "title", "score"), show="headings", height=10)
        for c, t, w in (("id", "TMDB ID", 100), ("media", "Type", 80), ("title", "Title", 420), ("score", "Score", 100)):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="w")
        tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        for r in candidate_rows:
            tree.insert("", tk.END, values=(r["tmdb_id"], r["media_type"], r["title"], f"{r['score']:.3f}"))

        def apply_selection():
            sel = tree.selection()
            if not sel:
                return
            vals = tree.item(sel[0], "values")
            tmdb_id = int(vals[0])
            media = str(vals[1])
            try:
                select_tmdb_candidate(conn, job_id, tmdb_id, media)
                show_job(job_id)
                dialog.destroy()
            except Exception as exc:
                messagebox.showerror("Override TMDB", str(exc))

        ttk.Button(dialog, text="Apply Selection", command=apply_selection).pack(pady=(0, 10))

    def run_mapping_override():
        mapping_id = simpledialog.askinteger("Override Mapping", "Mapping ID:", parent=root)
        ep_start = simpledialog.askinteger("Override Mapping", "Episode start:", parent=root)
        ep_end = simpledialog.askinteger("Override Mapping", "Episode end:", parent=root)
        if mapping_id is None or ep_start is None or ep_end is None:
            return
        if ep_end < ep_start:
            messagebox.showerror("Override Mapping", "episode_end must be >= episode_start")
            return
        try:
            ids = list(range(ep_start, ep_end + 1))
            set_mapping_override(conn, cfg, mapping_id, ep_start, ep_end, ids, "manual_override_gui")
            sid = selected_id()
            if sid:
                show_job(sid)
        except Exception as exc:
            messagebox.showerror("Override Mapping", str(exc))

    def run_split_override():
        split_plan_id = simpledialog.askinteger("Override Split", "Split plan ID:", parent=root)
        start = simpledialog.askfloat("Override Split", "Start seconds:", parent=root)
        end = simpledialog.askfloat("Override Split", "End seconds:", parent=root)
        if split_plan_id is None:
            return
        try:
            set_manual_split_timestamps(conn, split_plan_id, start, end)
            sid = selected_id()
            if sid:
                show_job(sid)
        except Exception as exc:
            messagebox.showerror("Override Split", str(exc))

    def run_file_override():
        mapping_id = simpledialog.askinteger("Override File", "Mapping ID:", parent=root)
        rip_title_id = simpledialog.askinteger("Override File", "Rip Title ID:", parent=root)
        if mapping_id is None or rip_title_id is None:
            return
        try:
            set_mapping_source_override(
                conn,
                mapping_id,
                rip_title_id,
                "manual_file_override_gui",
            )
            sid = selected_id()
            if sid:
                show_job(sid)
        except Exception as exc:
            messagebox.showerror("Override File", str(exc))

    def _set_current_job_partial_first_ten():
        sid = selected_id()
        if not sid:
            return
        try:
            update_job_disc_profile(conn, sid, "partial_season", 1, 1, 10)
            show_job(sid)
        except Exception as exc:
            messagebox.showerror("Set Partial Disc", str(exc))

    def _selected_artifact_path() -> str | None:
        sel = artifacts_tree.selection()
        if not sel:
            return None
        vals = artifacts_tree.item(sel[0], "values")
        if not vals or len(vals) < 2:
            return None
        path = str(vals[1] or "")
        return path if Path(path).exists() else None

    def open_selected_artifact():
        path = _selected_artifact_path()
        if not path:
            messagebox.showerror("Open Artifact", "Select an artifact row with a valid file path first.")
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("Open Artifact", str(exc))

    def copy_selected_artifact():
        path = _selected_artifact_path()
        if not path:
            messagebox.showerror("Copy Path", "Select an artifact row with a valid file path first.")
            return
        root.clipboard_clear()
        root.clipboard_append(path)

    jobs_tree.bind("<<TreeviewSelect>>", on_select)
    btn_refresh.configure(command=refresh_jobs)
    btn_start.configure(command=start_end_to_end)
    btn_resume.configure(command=resume_selected)
    btn_delete.configure(command=delete_selected)
    btn_identify.configure(command=run_identify)
    btn_map.configure(command=run_mapping)
    btn_menu.configure(command=run_menu_analysis)
    btn_select_tmdb.configure(command=run_tmdb_override)
    btn_map_override.configure(command=run_mapping_override)
    btn_file_override.configure(command=run_file_override)
    btn_split_override.configure(command=run_split_override)
    btn_open_artifact.configure(command=open_selected_artifact)
    btn_copy_artifact.configure(command=copy_selected_artifact)

    def tick():
        refresh_jobs()
        root.after(max(1, refresh_seconds) * 1000, tick)

    refresh_jobs()
    _update_button_states()
    tick()
    root.mainloop()


def _pick_disc_label() -> str:
    drives = discover_optical_drives()
    with_media = [d for d in drives if d.get("has_media")]
    if with_media:
        first = with_media[0]
        label = str(first.get("volume_label") or "").strip()
        if label:
            return label
    return ""


def _build_rip_monitor(staging_root_value: str, job_id: str, status: str) -> dict:
    # This function is intentionally pure from GUI perspective; it reads live FS data.
    from pathlib import Path

    if not staging_root_value:
        return {"active": False, "message": "No staging root configured."}
    staging_root = Path(str(staging_root_value))

    rip_dir = staging_root / "jobs" / job_id / "rip_output"
    log_file = staging_root / "jobs" / job_id / "logs" / "makemkv.log"
    if not rip_dir.exists():
        return {"active": status == "ripping", "message": "Rip output folder not created yet."}

    files = list(rip_dir.glob("*"))
    total_bytes = 0
    latest_mtime = None
    for f in files:
        try:
            st = f.stat()
        except OSError:
            continue
        total_bytes += int(st.st_size)
        latest_mtime = st.st_mtime if latest_mtime is None else max(latest_mtime, st.st_mtime)
    total_mb = round(total_bytes / (1024 * 1024), 1)

    stale_seconds = None
    if latest_mtime is not None:
        stale_seconds = int(datetime.now(timezone.utc).timestamp() - latest_mtime)

    log_tail = ""
    if log_file.exists():
        try:
            txt = log_file.read_text(encoding="utf-8", errors="replace")
            log_tail = "\n".join(txt.splitlines()[-2:])
        except Exception:
            pass

    return {
        "active": status == "ripping",
        "file_count": len(files),
        "size_mb": total_mb,
        "stale_seconds": stale_seconds,
        "log_tail": log_tail,
    }


def _load_job_artifact_json(staging_root_value: str, job_id: str, *parts: str):
    path = Path(str(staging_root_value)) / "jobs" / job_id
    for part in parts:
        path = path / part
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _display_episode_titles(value) -> str:
    if not value:
        return "-"
    if isinstance(value, list):
        return " / ".join(str(v) for v in value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return " / ".join(str(v) for v in parsed)
        except json.JSONDecodeError:
            return value
    return str(value)
