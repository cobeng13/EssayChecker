#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import BOTH, DISABLED, END, NORMAL
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

try:
    import yaml
except Exception:
    yaml = None

from csv_ingest import CsvPreview, build_students, load_preview, preview_rows_for_tree
from export import (
    export_gradebook_final,
    export_lms_upload_csv,
    export_summary_per_question,
    load_existing_result,
    prepare_run_dir,
    write_log,
    write_per_question_outputs,
    write_per_student_reports,
    write_run_config,
)
from grader import (
    DEFAULT_MODEL,
    DEFAULT_SYSTEM_INSTRUCTION,
    DEFAULT_TEMPERATURE,
    MODEL_PRICING_USD_PER_1M,
    estimate_call_tokens,
    grade_response,
    make_client,
    model_supports_custom_temperature,
    openai_available,
)
from questions import QuestionConfig, auto_map_questions, next_question_id
from utils import APP_VERSION, app_dir, hash_text


NONE_SENTINEL = "(None)"
MODEL_PICKER_OPTIONS = [
    ("gpt-4.1-mini", "gpt-4.1-mini - Stable default for rubric grading"),
    ("gpt-5-mini", "gpt-5-mini - Balanced quality, default temperature only"),
    ("gpt-4o-mini", "gpt-4o-mini - Lowest cost, weaker grading judgment"),
    ("gpt-4.1", "gpt-4.1 - Higher grading quality, higher cost"),
    ("gpt-4o", "gpt-4o - Strong grading quality, highest cost"),
]
MODEL_LABEL_TO_ID = {label: model_id for model_id, label in MODEL_PICKER_OPTIONS}
MODEL_ID_TO_LABEL = {model_id: label for model_id, label in MODEL_PICKER_OPTIONS}


@dataclass
class AppState:
    csv_path: str = ""
    output_folder: str = ""
    create_timestamped_subfolder: bool = True
    preview: CsvPreview | None = None
    selected_first_col: str | None = None
    selected_last_col: str | None = None
    selected_name_col: str | None = None
    selected_id_col: str | None = None
    selected_response_cols: list[str] = field(default_factory=list)
    questions: list[QuestionConfig] = field(default_factory=list)


@dataclass
class AppDefaults:
    output_folder: str = ""
    create_timestamped_subfolder: bool = True
    api_key: str = ""
    model: str = DEFAULT_MODEL
    temperature: float = DEFAULT_TEMPERATURE
    resume_if_graded: bool = True
    export_per_question_files: bool = False
    export_per_student_combined: bool = True
    system_instruction: str = DEFAULT_SYSTEM_INSTRUCTION


def app_config_path() -> Path:
    return app_dir() / "app_config.yaml"


def defaults_to_config_payload(defaults: AppDefaults) -> dict:
    return {
        "output_folder": defaults.output_folder,
        "create_timestamped_subfolder": defaults.create_timestamped_subfolder,
        "api_key": defaults.api_key,
        "run_defaults": {
            "model": defaults.model,
            "temperature": defaults.temperature,
            "resume_if_graded": defaults.resume_if_graded,
            "export_per_question_files": defaults.export_per_question_files,
            "export_per_student_combined": defaults.export_per_student_combined,
            "system_instruction": defaults.system_instruction,
        },
    }


def _yaml_scalar(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def dump_defaults_yaml(defaults: AppDefaults) -> str:
    payload = defaults_to_config_payload(defaults)
    if yaml is not None:
        return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)

    lines = [
        f"output_folder: {_yaml_scalar(payload['output_folder'])}",
        f"create_timestamped_subfolder: {'true' if payload['create_timestamped_subfolder'] else 'false'}",
        f"api_key: {_yaml_scalar(payload['api_key'])}",
        "",
        "run_defaults:",
        f"  model: {_yaml_scalar(payload['run_defaults']['model'])}",
        f"  temperature: {payload['run_defaults']['temperature']}",
        f"  resume_if_graded: {'true' if payload['run_defaults']['resume_if_graded'] else 'false'}",
        f"  export_per_question_files: {'true' if payload['run_defaults']['export_per_question_files'] else 'false'}",
        f"  export_per_student_combined: {'true' if payload['run_defaults']['export_per_student_combined'] else 'false'}",
        "  system_instruction: |",
    ]
    system_instruction = payload["run_defaults"]["system_instruction"] or ""
    if system_instruction:
        lines.extend([f"    {line}" for line in system_instruction.splitlines()])
    else:
        lines.append("    ")
    return "\n".join(lines) + "\n"


def write_app_defaults(defaults: AppDefaults):
    app_config_path().write_text(dump_defaults_yaml(defaults), encoding="utf-8")


def pick_text_file_and_read(parent, title: str) -> str | None:
    path = filedialog.askopenfilename(
        parent=parent,
        title=title,
        filetypes=[("Text files", "*.txt"), ("Markdown files", "*.md"), ("All files", "*.*")],
    )
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception as e:
        messagebox.showerror("File error", f"Could not read file:\n{e}", parent=parent)
        return None


def load_app_defaults() -> tuple[AppDefaults, list[str]]:
    defaults = AppDefaults()
    warnings: list[str] = []
    config_paths = [app_dir() / "app_config.yaml", app_dir() / "app_config.yml"]
    config_path = next((path for path in config_paths if path.exists()), None)
    if not config_path:
        return defaults, warnings
    if yaml is None:
        warnings.append(f"Config file found at {config_path.name}, but PyYAML is not installed. Run: pip install pyyaml")
        return defaults, warnings
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        warnings.append(f"Could not read {config_path.name}: {e}")
        return defaults, warnings
    if not isinstance(raw, dict):
        warnings.append(f"{config_path.name} must contain a top-level mapping.")
        return defaults, warnings

    run_cfg = raw.get("run_defaults", {})
    if run_cfg is None:
        run_cfg = {}
    if not isinstance(run_cfg, dict):
        warnings.append(f"{config_path.name}: run_defaults must be a mapping.")
        run_cfg = {}

    output_folder = raw.get("output_folder", defaults.output_folder)
    if isinstance(output_folder, str):
        defaults.output_folder = output_folder.strip()

    create_timestamped = raw.get("create_timestamped_subfolder", defaults.create_timestamped_subfolder)
    if isinstance(create_timestamped, bool):
        defaults.create_timestamped_subfolder = create_timestamped

    api_key = raw.get("api_key", defaults.api_key)
    if isinstance(api_key, str):
        defaults.api_key = api_key.strip()

    model = run_cfg.get("model", defaults.model)
    if model in MODEL_ID_TO_LABEL:
        defaults.model = model
    elif model:
        warnings.append(f"{config_path.name}: unsupported model '{model}' ignored.")

    temperature = run_cfg.get("temperature", defaults.temperature)
    try:
        defaults.temperature = float(temperature)
    except Exception:
        warnings.append(f"{config_path.name}: invalid temperature '{temperature}' ignored.")

    resume_if_graded = run_cfg.get("resume_if_graded", defaults.resume_if_graded)
    if isinstance(resume_if_graded, bool):
        defaults.resume_if_graded = resume_if_graded

    export_per_question_files = run_cfg.get("export_per_question_files", defaults.export_per_question_files)
    if isinstance(export_per_question_files, bool):
        defaults.export_per_question_files = export_per_question_files

    export_per_student_combined = run_cfg.get("export_per_student_combined", defaults.export_per_student_combined)
    if isinstance(export_per_student_combined, bool):
        defaults.export_per_student_combined = export_per_student_combined

    system_instruction = run_cfg.get("system_instruction", defaults.system_instruction)
    if isinstance(system_instruction, str) and system_instruction.strip():
        defaults.system_instruction = system_instruction.strip()

    return defaults, warnings


class Step1Frame(ttk.Frame):
    def __init__(self, parent, state: AppState, defaults: AppDefaults):
        super().__init__(parent)
        self.state = state
        self.defaults = defaults
        self.csv_var = tk.StringVar()
        self.out_var = tk.StringVar(value=defaults.output_folder)
        self.timestamp_var = tk.BooleanVar(value=defaults.create_timestamped_subfolder)
        self.det_name_var = tk.StringVar(value="Name columns: (not loaded)")
        self.det_resp_var = tk.StringVar(value="Response columns: (not loaded)")
        self.det_id_var = tk.StringVar(value="ID column: (not loaded)")
        self.first_var = tk.StringVar(value=NONE_SENTINEL)
        self.last_var = tk.StringVar(value=NONE_SENTINEL)
        self.name_var = tk.StringVar(value=NONE_SENTINEL)
        self.id_var = tk.StringVar(value=NONE_SENTINEL)
        self.response_listbox: tk.Listbox | None = None
        self.preview_tree: ttk.Treeview | None = None
        self.manual_frame: ttk.LabelFrame | None = None
        self.manual_toggle_btn: ttk.Button | None = None
        self.manual_visible = False
        self.loaded_csv_path = ""
        self._build_ui()

    def _build_ui(self):
        top = ttk.LabelFrame(self, text="Step 1: Select CSV + Output Folder")
        top.pack(fill="x", padx=8, pady=8)

        row1 = ttk.Frame(top)
        row1.pack(fill="x", padx=8, pady=6)
        ttk.Label(row1, text="LMS CSV file:").pack(side="left")
        ttk.Entry(row1, textvariable=self.csv_var, width=80).pack(side="left", padx=6, fill="x", expand=True)
        ttk.Button(row1, text="Browse", command=self._pick_csv).pack(side="left")

        row2 = ttk.Frame(top)
        row2.pack(fill="x", padx=8, pady=6)
        ttk.Label(row2, text="Results folder:").pack(side="left")
        ttk.Entry(row2, textvariable=self.out_var, width=80).pack(side="left", padx=6, fill="x", expand=True)
        ttk.Button(row2, text="Browse", command=self._pick_out).pack(side="left")

        row3 = ttk.Frame(top)
        row3.pack(fill="x", padx=8, pady=6)
        ttk.Checkbutton(row3, text="Create timestamped subfolder", variable=self.timestamp_var).pack(side="left")
        ttk.Button(row3, text="Reload Preview + Auto-detect", command=self.load_and_detect).pack(side="right")

        detect = ttk.LabelFrame(self, text="Auto-detection")
        detect.pack(fill="x", padx=8, pady=6)
        ttk.Label(detect, textvariable=self.det_name_var).pack(anchor="w", padx=8, pady=2)
        ttk.Label(detect, textvariable=self.det_id_var).pack(anchor="w", padx=8, pady=2)
        ttk.Label(detect, textvariable=self.det_resp_var).pack(anchor="w", padx=8, pady=2)

        manual_toggle_row = ttk.Frame(self)
        manual_toggle_row.pack(fill="x", padx=8, pady=(0, 6))
        self.manual_toggle_btn = ttk.Button(
            manual_toggle_row,
            text="Show Manual Overrides",
            command=self._toggle_manual_overrides,
        )
        self.manual_toggle_btn.pack(side="left")

        self.manual_frame = ttk.LabelFrame(self, text="Manual overrides")
        rowm1 = ttk.Frame(self.manual_frame)
        rowm1.pack(fill="x", padx=8, pady=4)
        ttk.Label(rowm1, text="First Name").pack(side="left")
        self.first_combo = ttk.Combobox(rowm1, textvariable=self.first_var, width=24, state="readonly")
        self.first_combo.pack(side="left", padx=4)
        ttk.Label(rowm1, text="Last Name").pack(side="left")
        self.last_combo = ttk.Combobox(rowm1, textvariable=self.last_var, width=24, state="readonly")
        self.last_combo.pack(side="left", padx=4)
        ttk.Label(rowm1, text="Name").pack(side="left")
        self.name_combo = ttk.Combobox(rowm1, textvariable=self.name_var, width=24, state="readonly")
        self.name_combo.pack(side="left", padx=4)

        rowm2 = ttk.Frame(self.manual_frame)
        rowm2.pack(fill="x", padx=8, pady=4)
        ttk.Label(rowm2, text="Student ID").pack(side="left")
        self.id_combo = ttk.Combobox(rowm2, textvariable=self.id_var, width=24, state="readonly")
        self.id_combo.pack(side="left", padx=4)
        ttk.Label(rowm2, text="Response columns").pack(side="left", padx=(10, 4))
        self.response_listbox = tk.Listbox(rowm2, selectmode="extended", width=70, height=5, exportselection=False)
        self.response_listbox.pack(side="left", fill="x", expand=True)

        prev = ttk.LabelFrame(self, text="Preview (first 10 rows)")
        prev.pack(fill=BOTH, expand=True, padx=8, pady=8)
        self.preview_tree = ttk.Treeview(prev, show="headings")
        self.preview_tree.pack(side="left", fill=BOTH, expand=True)
        sy = ttk.Scrollbar(prev, orient="vertical", command=self.preview_tree.yview)
        sy.pack(side="right", fill="y")
        sx = ttk.Scrollbar(self, orient="horizontal", command=self.preview_tree.xview)
        sx.pack(fill="x", padx=8)
        self.preview_tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)

    def _pick_csv(self):
        path = filedialog.askopenfilename(title="Select LMS CSV", filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if path:
            self.csv_var.set(path)
            if not self.out_var.get().strip():
                self.out_var.set(str(Path(path).parent))
            self.load_and_detect()

    def _pick_out(self):
        path = filedialog.askdirectory(title="Select results folder")
        if path:
            self.out_var.set(path)

    def _combo_value(self, value: str) -> str | None:
        v = (value or "").strip()
        return None if not v or v == NONE_SENTINEL else v

    def _toggle_manual_overrides(self):
        if self.manual_visible:
            if self.manual_frame:
                self.manual_frame.pack_forget()
            self.manual_visible = False
            if self.manual_toggle_btn:
                self.manual_toggle_btn.config(text="Show Manual Overrides")
            return
        if self.manual_frame:
            self.manual_frame.pack(fill="x", padx=8, pady=6, before=self.preview_tree.master if self.preview_tree else None)
        self.manual_visible = True
        if self.manual_toggle_btn:
            self.manual_toggle_btn.config(text="Hide Manual Overrides")

    def _set_manual_visibility(self, visible: bool):
        if visible != self.manual_visible:
            self._toggle_manual_overrides()

    def load_and_detect(self):
        csv_path = self.csv_var.get().strip()
        if not csv_path:
            messagebox.showwarning("CSV required", "Please choose a CSV file first.")
            return
        try:
            prev = load_preview(csv_path, max_rows=10)
        except Exception as e:
            messagebox.showerror("CSV error", str(e))
            return

        self.state.preview = prev
        self.loaded_csv_path = csv_path
        headers = prev.headers
        combo_values = [NONE_SENTINEL] + headers
        self.first_combo["values"] = combo_values
        self.last_combo["values"] = combo_values
        self.name_combo["values"] = combo_values
        self.id_combo["values"] = combo_values
        self.first_var.set(prev.first_col or NONE_SENTINEL)
        self.last_var.set(prev.last_col or NONE_SENTINEL)
        self.name_var.set(prev.name_col or NONE_SENTINEL)
        self.id_var.set(prev.id_col or NONE_SENTINEL)

        if self.response_listbox:
            self.response_listbox.delete(0, END)
            for c in headers:
                self.response_listbox.insert(END, c)
            for i, c in enumerate(headers):
                if c in prev.response_cols:
                    self.response_listbox.selection_set(i)

        self.det_name_var.set(
            f"Name columns: first={prev.first_col or 'None'}, last={prev.last_col or 'None'}, name={prev.name_col or 'None'}"
        )
        self.det_id_var.set(f"ID column: {prev.id_col or 'None'}")
        self.det_resp_var.set(f"Response columns: {', '.join(prev.response_cols) if prev.response_cols else 'None'}")
        should_show_manual = not prev.response_cols or not ((prev.first_col and prev.last_col) or prev.name_col or prev.first_col or prev.last_col)
        self._set_manual_visibility(should_show_manual)
        self._fill_preview_tree(prev)

    def _fill_preview_tree(self, prev: CsvPreview):
        if not self.preview_tree:
            return
        self.preview_tree.delete(*self.preview_tree.get_children())
        self.preview_tree["columns"] = prev.headers
        for h in prev.headers:
            self.preview_tree.heading(h, text=h)
            self.preview_tree.column(h, width=130, anchor="w")
        for row in preview_rows_for_tree(prev):
            self.preview_tree.insert("", END, values=row)

    def validate_and_commit(self) -> bool:
        csv_path = self.csv_var.get().strip()
        out = self.out_var.get().strip()
        if not csv_path:
            messagebox.showwarning("Missing CSV", "Please select an input CSV.")
            return False
        if not Path(csv_path).exists():
            messagebox.showwarning("Missing CSV", "The selected CSV path does not exist.")
            return False
        if not out:
            messagebox.showwarning("Missing output folder", "Please select an output folder.")
            return False
        if self.state.preview is None or self.loaded_csv_path != csv_path:
            self.load_and_detect()
            if self.state.preview is None:
                return False

        selected_responses: list[str] = []
        if self.response_listbox:
            for i in self.response_listbox.curselection():
                selected_responses.append(self.response_listbox.get(i))
        if not selected_responses:
            messagebox.showwarning("Missing response columns", "Select at least one response column.")
            return False

        first_col = self._combo_value(self.first_var.get())
        last_col = self._combo_value(self.last_var.get())
        name_col = self._combo_value(self.name_var.get())
        if not ((first_col and last_col) or name_col or first_col or last_col):
            messagebox.showwarning("Missing name mapping", "Select First/Last columns or a Name column.")
            return False

        self.state.csv_path = csv_path
        self.state.output_folder = out
        self.state.create_timestamped_subfolder = bool(self.timestamp_var.get())
        self.state.selected_first_col = first_col
        self.state.selected_last_col = last_col
        self.state.selected_name_col = name_col
        self.state.selected_id_col = self._combo_value(self.id_var.get())
        self.state.selected_response_cols = selected_responses
        return True

    def on_show(self):
        if self.state.csv_path:
            self.csv_var.set(self.state.csv_path)
        if self.state.output_folder:
            self.out_var.set(self.state.output_folder)
        self.timestamp_var.set(self.state.create_timestamped_subfolder)


class Step2Frame(ttk.Frame):
    def __init__(self, parent, state: AppState, get_preset_names):
        super().__init__(parent)
        self.state = state
        self.get_preset_names = get_preset_names
        self.tree: ttk.Treeview | None = None
        self.qid_var = tk.StringVar()
        self.cols_var = tk.StringVar()
        self.max_var = tk.StringVar(value="10")
        self.preset_var = tk.StringVar(value="")
        self.summary_var = tk.StringVar(value="Question preview unavailable until Step 1 is configured.")
        self.manual_tools_visible = False
        self.manual_frame: ttk.LabelFrame | None = None
        self.manual_toggle_btn: ttk.Button | None = None
        self._build_ui()

    def _build_ui(self):
        top = ttk.LabelFrame(self, text="Step 2: Question Preview")
        top.pack(fill="x", padx=8, pady=8)

        summary = ttk.Frame(top)
        summary.pack(fill="x", padx=8, pady=6)
        ttk.Label(summary, textvariable=self.summary_var, justify="left", wraplength=1080).pack(anchor="w")

        btns = ttk.Frame(top)
        btns.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Button(btns, text="Refresh auto-map", command=self.auto_map).pack(side="left")
        self.manual_toggle_btn = ttk.Button(btns, text="Show Manual Question Tools", command=self._toggle_manual_tools)
        self.manual_toggle_btn.pack(side="left", padx=8)

        self.tree = ttk.Treeview(top, columns=("qid", "cols", "max", "preset"), show="headings", selectmode="extended", height=10)
        self.tree.pack(fill=BOTH, expand=True, padx=8, pady=6)
        self.tree.heading("qid", text="Question ID")
        self.tree.heading("cols", text="Uses columns")
        self.tree.heading("max", text="Max points")
        self.tree.heading("preset", text="Rubric preset")
        self.tree.column("qid", width=100)
        self.tree.column("cols", width=500)
        self.tree.column("max", width=100)
        self.tree.column("preset", width=180)
        self.tree.bind("<<TreeviewSelect>>", self._load_selected_to_editor)

        self.manual_frame = ttk.LabelFrame(self, text="Manual question tools")
        tool_btns = ttk.Frame(self.manual_frame)
        tool_btns.pack(fill="x", padx=8, pady=6)
        ttk.Button(tool_btns, text="Add question", command=self.add_question).pack(side="left")
        ttk.Button(tool_btns, text="Remove question", command=self.remove_selected).pack(side="left", padx=4)
        ttk.Button(tool_btns, text="Merge selected", command=self.merge_selected).pack(side="left", padx=4)
        ttk.Button(tool_btns, text="Move up", command=lambda: self.move_selected(-1)).pack(side="left", padx=4)
        ttk.Button(tool_btns, text="Move down", command=lambda: self.move_selected(1)).pack(side="left", padx=4)

        edit = ttk.Frame(self.manual_frame)
        edit.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(edit, text="Question ID").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(edit, textvariable=self.qid_var, width=16).grid(row=0, column=1, sticky="w", padx=4, pady=4)
        ttk.Label(edit, text="Uses columns (comma-separated)").grid(row=0, column=2, sticky="w", padx=4, pady=4)
        ttk.Entry(edit, textvariable=self.cols_var, width=60).grid(row=0, column=3, sticky="w", padx=4, pady=4)
        ttk.Label(edit, text="Max points").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(edit, textvariable=self.max_var, width=16).grid(row=1, column=1, sticky="w", padx=4, pady=4)
        ttk.Label(edit, text="Rubric preset").grid(row=1, column=2, sticky="w", padx=4, pady=4)
        self.preset_combo = ttk.Combobox(edit, textvariable=self.preset_var, width=30)
        self.preset_combo.grid(row=1, column=3, sticky="w", padx=4, pady=4)
        ttk.Button(edit, text="Save selected row", command=self.save_selected_row).grid(row=1, column=4, padx=8)

    def _toggle_manual_tools(self):
        if self.manual_tools_visible:
            if self.manual_frame:
                self.manual_frame.pack_forget()
            self.manual_tools_visible = False
            if self.manual_toggle_btn:
                self.manual_toggle_btn.config(text="Show Manual Question Tools")
            return
        if self.manual_frame:
            self.manual_frame.pack(fill="x", padx=8, pady=8)
        self.manual_tools_visible = True
        if self.manual_toggle_btn:
            self.manual_toggle_btn.config(text="Hide Manual Question Tools")

    def _update_summary(self):
        question_count = len(self.state.questions)
        response_count = len(self.state.selected_response_cols)
        if not question_count:
            self.summary_var.set("No questions mapped yet. Use Refresh auto-map after Step 1 detection.")
            return
        merged_count = sum(1 for q in self.state.questions if len(q.columns) > 1)
        preview_lines = [
            f"Auto-mapped {question_count} question(s) from {response_count} response column(s).",
            f"Merged question groups: {merged_count}.",
        ]
        if self.state.questions:
            first = self.state.questions[0]
            preview_lines.append(f"First question preview: {first.question_id} from {', '.join(first.columns)}.")
        self.summary_var.set(" ".join(preview_lines))

    def _refresh_tree(self):
        if not self.tree:
            return
        self.tree.delete(*self.tree.get_children())
        for i, q in enumerate(self.state.questions):
            self.tree.insert("", END, iid=str(i), values=(q.question_id, ", ".join(q.columns), q.max_points, q.rubric_preset))
        self.preset_combo["values"] = self.get_preset_names()
        self._update_summary()

    def auto_map(self):
        self.state.questions = auto_map_questions(self.state.selected_response_cols)
        self._refresh_tree()

    def add_question(self):
        qid = next_question_id(self.state.questions)
        default_col = self.state.selected_response_cols[0] if self.state.selected_response_cols else ""
        cols = [default_col] if default_col else []
        self.state.questions.append(QuestionConfig(question_id=qid, columns=cols, max_points=10.0))
        self._refresh_tree()

    def remove_selected(self):
        if not self.tree:
            return
        sel = sorted([int(i) for i in self.tree.selection()], reverse=True)
        for idx in sel:
            if 0 <= idx < len(self.state.questions):
                self.state.questions.pop(idx)
        self._refresh_tree()

    def merge_selected(self):
        if not self.tree:
            return
        sel = sorted([int(i) for i in self.tree.selection()])
        if len(sel) < 2:
            messagebox.showinfo("Merge", "Select at least 2 question rows to merge.")
            return
        first = sel[0]
        merged_cols: list[str] = []
        for idx in sel:
            for c in self.state.questions[idx].columns:
                if c not in merged_cols:
                    merged_cols.append(c)
        self.state.questions[first].columns = merged_cols
        for idx in reversed(sel[1:]):
            self.state.questions.pop(idx)
        self._refresh_tree()

    def move_selected(self, direction: int):
        if not self.tree:
            return
        sel = self.tree.selection()
        if len(sel) != 1:
            return
        idx = int(sel[0])
        target = idx + direction
        if target < 0 or target >= len(self.state.questions):
            return
        self.state.questions[idx], self.state.questions[target] = self.state.questions[target], self.state.questions[idx]
        self._refresh_tree()
        self.tree.selection_set(str(target))

    def _load_selected_to_editor(self, _event=None):
        if not self.tree:
            return
        sel = self.tree.selection()
        if len(sel) != 1:
            return
        q = self.state.questions[int(sel[0])]
        self.qid_var.set(q.question_id)
        self.cols_var.set(", ".join(q.columns))
        self.max_var.set(str(q.max_points))
        self.preset_var.set(q.rubric_preset)

    def save_selected_row(self):
        if not self.tree:
            return
        sel = self.tree.selection()
        if len(sel) != 1:
            messagebox.showwarning("Select one", "Select exactly one row to edit.")
            return
        idx = int(sel[0])
        qid = self.qid_var.get().strip() or self.state.questions[idx].question_id
        cols = [x.strip() for x in self.cols_var.get().split(",") if x.strip()]
        if not cols:
            messagebox.showwarning("Missing columns", "Question must include at least one source column.")
            return
        for c in cols:
            if c not in self.state.selected_response_cols:
                messagebox.showwarning("Invalid column", f"Column '{c}' is not in selected response columns.")
                return
        try:
            max_points = float(self.max_var.get().strip())
        except Exception:
            messagebox.showwarning("Invalid max points", "Max points must be numeric.")
            return
        self.state.questions[idx].question_id = qid
        self.state.questions[idx].columns = cols
        self.state.questions[idx].max_points = max_points
        self.state.questions[idx].rubric_preset = self.preset_var.get().strip()
        self._refresh_tree()
        self.tree.selection_set(str(idx))

    def validate_and_commit(self) -> bool:
        if not self.state.questions:
            messagebox.showwarning("No questions", "Add or auto-map at least one question.")
            return False
        ids = set()
        for q in self.state.questions:
            if not q.question_id.strip():
                messagebox.showwarning("Invalid question", "Question ID cannot be empty.")
                return False
            if q.question_id in ids:
                messagebox.showwarning("Duplicate ID", f"Duplicate Question ID: {q.question_id}")
                return False
            ids.add(q.question_id)
            if not q.columns:
                messagebox.showwarning("Missing columns", f"{q.question_id} has no mapped columns.")
                return False
        return True

    def on_show(self):
        if not self.state.questions and self.state.selected_response_cols:
            self.state.questions = auto_map_questions(self.state.selected_response_cols)
        self._refresh_tree()


class Step3Frame(ttk.Frame):
    def __init__(self, parent, state: AppState, get_presets, save_presets):
        super().__init__(parent)
        self.state = state
        self.get_presets = get_presets
        self.save_presets = save_presets
        self.current_index: int | None = None
        self.max_var = tk.StringVar(value="10")
        self.preset_choice_var = tk.StringVar(value="")
        self._build_ui()

    def _build_ui(self):
        wrapper = ttk.LabelFrame(self, text="Step 3: Rubrics & Model Answers")
        wrapper.pack(fill=BOTH, expand=True, padx=8, pady=8)

        left = ttk.Frame(wrapper)
        left.pack(side="left", fill="y", padx=8, pady=8)
        ttk.Label(left, text="Questions").pack(anchor="w")
        self.q_list = tk.Listbox(left, width=20, exportselection=False)
        self.q_list.pack(fill="y", expand=True)
        self.q_list.bind("<<ListboxSelect>>", self._on_q_select)

        right = ttk.Frame(wrapper)
        right.pack(side="left", fill=BOTH, expand=True, padx=8, pady=8)
        rubric_row = ttk.Frame(right)
        rubric_row.pack(fill="x")
        ttk.Label(rubric_row, text="Rubric (required)").pack(side="left")
        ttk.Button(rubric_row, text="Load .txt", command=lambda: self._load_text_into(self.rubric_txt, "Load rubric text")).pack(
            side="left", padx=8
        )
        self.rubric_txt = ScrolledText(right, height=10, wrap="word")
        self.rubric_txt.pack(fill=BOTH, expand=True, pady=(0, 8))

        model_row = ttk.Frame(right)
        model_row.pack(fill="x")
        ttk.Label(model_row, text="Model answer (optional)").pack(side="left")
        ttk.Button(
            model_row,
            text="Load .txt",
            command=lambda: self._load_text_into(self.model_txt, "Load model answer text"),
        ).pack(side="left", padx=8)
        self.model_txt = ScrolledText(right, height=7, wrap="word")
        self.model_txt.pack(fill=BOTH, expand=True, pady=(0, 8))

        extra_row = ttk.Frame(right)
        extra_row.pack(fill="x")
        ttk.Label(extra_row, text="Extra instructions (optional)").pack(side="left")
        ttk.Button(
            extra_row,
            text="Load .txt",
            command=lambda: self._load_text_into(self.extra_txt, "Load extra instructions text"),
        ).pack(side="left", padx=8)
        self.extra_txt = ScrolledText(right, height=6, wrap="word")
        self.extra_txt.pack(fill=BOTH, expand=True, pady=(0, 8))

        rowmax = ttk.Frame(right)
        rowmax.pack(fill="x", pady=4)
        ttk.Label(rowmax, text="Max points").pack(side="left")
        ttk.Entry(rowmax, textvariable=self.max_var, width=10).pack(side="left", padx=6)
        ttk.Button(rowmax, text="Save question edits", command=self._save_current_question).pack(side="left", padx=8)

        preset_row = ttk.Frame(right)
        preset_row.pack(fill="x", pady=6)
        ttk.Button(preset_row, text="Apply current to all questions", command=self._apply_current_to_all).pack(side="left")
        ttk.Button(preset_row, text="Save as preset", command=self._save_as_preset).pack(side="left", padx=6)
        self.preset_combo = ttk.Combobox(preset_row, textvariable=self.preset_choice_var, width=34)
        self.preset_combo.pack(side="left", padx=6)
        ttk.Button(preset_row, text="Load preset into current", command=self._load_preset_into_current).pack(side="left", padx=6)

    def _refresh_list(self):
        self.q_list.delete(0, END)
        for q in self.state.questions:
            self.q_list.insert(END, q.question_id)
        self.preset_combo["values"] = sorted(self.get_presets().keys())

    def _load_text_into(self, widget: ScrolledText, title: str):
        content = pick_text_file_and_read(self, title)
        if content is None:
            return
        widget.delete("1.0", END)
        widget.insert("1.0", content)

    def _save_current_question(self):
        if self.current_index is None:
            return
        q = self.state.questions[self.current_index]
        q.rubric = self.rubric_txt.get("1.0", END).strip()
        q.model_answer = self.model_txt.get("1.0", END).strip()
        q.extra_instructions = self.extra_txt.get("1.0", END).strip()
        try:
            q.max_points = float(self.max_var.get().strip())
        except Exception:
            messagebox.showwarning("Invalid max points", "Max points must be numeric.")
            return

    def _load_question(self, idx: int):
        q = self.state.questions[idx]
        self.rubric_txt.delete("1.0", END)
        self.rubric_txt.insert("1.0", q.rubric)
        self.model_txt.delete("1.0", END)
        self.model_txt.insert("1.0", q.model_answer)
        self.extra_txt.delete("1.0", END)
        self.extra_txt.insert("1.0", q.extra_instructions)
        self.max_var.set(str(q.max_points))
        self.current_index = idx

    def _on_q_select(self, _event=None):
        sel = self.q_list.curselection()
        if not sel:
            return
        self._save_current_question()
        self._load_question(sel[0])

    def _apply_current_to_all(self):
        if self.current_index is None:
            return
        self._save_current_question()
        src = self.state.questions[self.current_index]
        for i, q in enumerate(self.state.questions):
            if i == self.current_index:
                continue
            q.rubric = src.rubric
            q.model_answer = src.model_answer
            q.extra_instructions = src.extra_instructions
            q.max_points = src.max_points
        messagebox.showinfo("Applied", "Current rubric/model/instructions applied to all questions.")

    def _save_as_preset(self):
        if self.current_index is None:
            return
        self._save_current_question()
        name = simpledialog.askstring("Preset name", "Preset name:")
        if not name:
            return
        name = name.strip()
        if not name:
            return
        q = self.state.questions[self.current_index]
        presets = self.get_presets()
        presets[name] = {
            "rubric": q.rubric,
            "model_answer": q.model_answer,
            "extra_instructions": q.extra_instructions,
            "default_max_points": q.max_points,
        }
        self.save_presets(presets)
        self.preset_combo["values"] = sorted(presets.keys())
        self.preset_choice_var.set(name)
        messagebox.showinfo("Saved", f"Preset '{name}' saved.")

    def _load_preset_into_current(self):
        if self.current_index is None:
            return
        name = self.preset_choice_var.get().strip()
        presets = self.get_presets()
        if name not in presets:
            messagebox.showwarning("Preset", "Select a valid preset name.")
            return
        p = presets[name]
        self.rubric_txt.delete("1.0", END)
        self.rubric_txt.insert("1.0", p.get("rubric", ""))
        self.model_txt.delete("1.0", END)
        self.model_txt.insert("1.0", p.get("model_answer", ""))
        self.extra_txt.delete("1.0", END)
        self.extra_txt.insert("1.0", p.get("extra_instructions", ""))
        self.max_var.set(str(p.get("default_max_points", 10)))
        self._save_current_question()
        self.state.questions[self.current_index].rubric_preset = name

    def validate_and_commit(self) -> bool:
        self._save_current_question()
        for q in self.state.questions:
            if not q.rubric.strip():
                messagebox.showwarning("Missing rubric", f"{q.question_id} requires rubric text.")
                return False
        return True

    def on_show(self):
        self._refresh_list()
        if self.state.questions:
            self.q_list.selection_clear(0, END)
            self.q_list.selection_set(0)
            self._on_q_select()


class Step4Frame(ttk.Frame):
    def __init__(self, parent, state: AppState, get_presets, defaults: AppDefaults, save_defaults_cb, reset_defaults_cb):
        super().__init__(parent)
        self.state = state
        self.get_presets = get_presets
        self.defaults = defaults
        self.save_defaults_cb = save_defaults_cb
        self.reset_defaults_cb = reset_defaults_cb
        initial_api_key = defaults.api_key or os.getenv("OPENAI_API_KEY", "")
        self.api_key_var = tk.StringVar(value=initial_api_key)
        self.model_var = tk.StringVar(value=defaults.model)
        self.model_choice_var = tk.StringVar(value=MODEL_ID_TO_LABEL.get(defaults.model, MODEL_PICKER_OPTIONS[0][1]))
        self.temp_var = tk.StringVar(value=str(defaults.temperature))
        self.resume_var = tk.BooleanVar(value=defaults.resume_if_graded)
        self.export_pq_var = tk.BooleanVar(value=defaults.export_per_question_files)
        self.export_ps_var = tk.BooleanVar(value=defaults.export_per_student_combined)
        self.progress_text_var = tk.StringVar(value="Ready.")
        self.estimate_var = tk.StringVar(value="Estimate unavailable until CSV/questions are configured.")
        self.pricing_note_var = tk.StringVar(value="")
        self.temperature_note_var = tk.StringVar(value="")
        self.running = False
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.msg_queue: queue.Queue[tuple[str, dict]] = queue.Queue()
        self.total_tasks = 0
        self.done_tasks = 0
        self._build_ui()
        self.after(150, self._poll_queue)

    def _build_ui(self):
        top = ttk.LabelFrame(self, text="Step 4: Run")
        top.pack(fill="x", padx=8, pady=8)
        row0 = ttk.Frame(top)
        row0.pack(fill="x", padx=8, pady=4)
        row0_head = ttk.Frame(row0)
        row0_head.pack(fill="x")
        ttk.Label(row0_head, text="Global System Instruction").pack(side="left")
        ttk.Button(row0_head, text="Load .txt", command=self._load_system_instruction_file).pack(side="left", padx=8)
        self.sys_text = ScrolledText(row0, height=6, wrap="word")
        self.sys_text.pack(fill="x", expand=True)
        self.sys_text.insert("1.0", self.defaults.system_instruction)

        row1 = ttk.Frame(top)
        row1.pack(fill="x", padx=8, pady=4)
        ttk.Label(row1, text="OpenAI API key").pack(side="left")
        self.api_entry = ttk.Entry(row1, textvariable=self.api_key_var, width=50, show="*")
        self.api_entry.pack(side="left", padx=6)
        ttk.Button(row1, text="Show/Hide", command=self._toggle_key).pack(side="left")

        row2 = ttk.Frame(top)
        row2.pack(fill="x", padx=8, pady=4)
        ttk.Label(row2, text="Model").pack(side="left")
        self.model_combo = ttk.Combobox(
            row2,
            textvariable=self.model_choice_var,
            values=[label for _, label in MODEL_PICKER_OPTIONS],
            width=26,
            state="readonly",
        )
        self.model_combo.pack(side="left", padx=6)
        self.model_combo.bind("<<ComboboxSelected>>", self._on_model_choice_changed)
        ttk.Label(row2, text="Temperature").pack(side="left", padx=(12, 4))
        self.temp_entry = ttk.Entry(row2, textvariable=self.temp_var, width=8)
        self.temp_entry.pack(side="left")

        row2b = ttk.Frame(top)
        row2b.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(
            row2b,
            textvariable=self.temperature_note_var,
            justify="left",
            wraplength=1080,
            foreground="#555555",
        ).pack(anchor="w")

        row3 = ttk.Frame(top)
        row3.pack(fill="x", padx=8, pady=4)
        ttk.Checkbutton(row3, text="Resume if already graded", variable=self.resume_var).pack(side="left")
        ttk.Checkbutton(row3, text="Export per-question files", variable=self.export_pq_var).pack(side="left", padx=10)
        ttk.Checkbutton(row3, text="Export per-student combined report", variable=self.export_ps_var).pack(side="left", padx=10)

        row4 = ttk.Frame(top)
        row4.pack(fill="x", padx=8, pady=6)
        self.start_btn = ttk.Button(row4, text="Start", command=self.start_run)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(row4, text="Stop", command=self.stop_run, state=DISABLED)
        self.stop_btn.pack(side="left", padx=8)
        ttk.Button(row4, text="Refresh estimate", command=self.refresh_estimate).pack(side="left", padx=(0, 8))
        ttk.Button(row4, text="Set as default settings", command=self.save_defaults_cb).pack(side="left", padx=(0, 8))
        ttk.Button(row4, text="Revert to stock configs", command=self.reset_defaults_cb).pack(side="left", padx=(0, 8))
        self.pbar = ttk.Progressbar(row4, length=380, mode="determinate")
        self.pbar.pack(side="left", padx=10)
        ttk.Label(row4, textvariable=self.progress_text_var).pack(side="left")

        estimate = ttk.LabelFrame(top, text="Approximate token usage and cost")
        estimate.pack(fill="x", padx=8, pady=6)
        ttk.Label(
            estimate,
            textvariable=self.estimate_var,
            justify="left",
            wraplength=1080,
        ).pack(anchor="w", padx=8, pady=(6, 2))
        ttk.Label(
            estimate,
            textvariable=self.pricing_note_var,
            justify="left",
            wraplength=1080,
            foreground="#555555",
        ).pack(anchor="w", padx=8, pady=(0, 6))

        log_frame = ttk.LabelFrame(self, text="Run log")
        log_frame.pack(fill=BOTH, expand=True, padx=8, pady=8)
        self.log_text = ScrolledText(log_frame, height=16, wrap="word", state=DISABLED)
        self.log_text.pack(fill=BOTH, expand=True)
        self._apply_model_selection()
        self.temp_var.trace_add("write", self._schedule_estimate_refresh)
        self.resume_var.trace_add("write", self._schedule_estimate_refresh)

    def _toggle_key(self):
        self.api_entry.config(show="" if self.api_entry.cget("show") else "*")

    def _load_system_instruction_file(self):
        content = pick_text_file_and_read(self, "Load global system instruction")
        if content is None:
            return
        self.sys_text.delete("1.0", END)
        self.sys_text.insert("1.0", content)
        self.refresh_estimate()

    def _resolved_model(self) -> str:
        choice_label = self.model_choice_var.get().strip()
        return MODEL_LABEL_TO_ID.get(choice_label, DEFAULT_MODEL)

    def _apply_model_selection(self):
        resolved = self._resolved_model()
        if resolved:
            self.model_var.set(resolved)
        else:
            self.model_var.set(DEFAULT_MODEL)
        self._apply_temperature_behavior()

    def _apply_temperature_behavior(self):
        model = self._resolved_model().strip() or DEFAULT_MODEL
        if model_supports_custom_temperature(model):
            self.temp_entry.config(state=NORMAL)
            self.temperature_note_var.set("")
            return
        self.temp_var.set("1")
        self.temp_entry.config(state=DISABLED)
        self.temperature_note_var.set(f"{model} uses the model default temperature only. The app will send no explicit temperature.")

    def _on_model_choice_changed(self, _event=None):
        self._apply_model_selection()
        self.refresh_estimate()

    def _schedule_estimate_refresh(self, *_args):
        self.after(100, self.refresh_estimate)

    def refresh_estimate(self):
        self._apply_model_selection()
        if not self.state.csv_path or not self.state.questions:
            self.estimate_var.set("Estimate unavailable until CSV and questions are configured.")
            self.pricing_note_var.set("")
            return

        try:
            _, students = build_students(
                self.state.csv_path,
                self.state.selected_first_col,
                self.state.selected_last_col,
                self.state.selected_name_col,
                self.state.selected_id_col,
                self.state.selected_response_cols,
            )
        except Exception as e:
            self.estimate_var.set(f"Estimate unavailable: {e}")
            self.pricing_note_var.set("")
            return

        system_instruction = self.sys_text.get("1.0", END).strip() or DEFAULT_SYSTEM_INSTRUCTION
        total_calls = 0
        total_input_tokens = 0
        total_output_tokens = 0

        for student in students:
            for q in self.state.questions:
                chunks = [student.responses_by_column.get(c, "") for c in q.columns]
                chunks = [c for c in chunks if c.strip()]
                if not chunks:
                    continue
                response_text = "\n\n-----\n\n".join(chunks).strip()
                input_tokens, output_tokens = estimate_call_tokens(
                    system_instruction=system_instruction,
                    rubric=q.rubric,
                    model_answer=q.model_answer,
                    extra_instructions=q.extra_instructions,
                    student_response=response_text,
                    question_id=q.question_id,
                    question_title=q.question_title,
                    max_points=q.max_points,
                )
                total_calls += 1
                total_input_tokens += input_tokens
                total_output_tokens += output_tokens

        model = self._resolved_model() or DEFAULT_MODEL
        self.model_var.set(model)
        pricing = MODEL_PRICING_USD_PER_1M.get(model)
        estimate_line = (
            f"Approximate API calls: {total_calls} | Input tokens: {total_input_tokens:,} | "
            f"Output tokens: {total_output_tokens:,}"
        )
        if pricing:
            usd = (
                (total_input_tokens / 1_000_000) * pricing["input"]
                + (total_output_tokens / 1_000_000) * pricing["output"]
            )
            estimate_line += f" | Estimated cost: ${usd:,.4f}"
            note = (
                f"Pricing basis for {model}: input ${pricing['input']}/1M, output ${pricing['output']}/1M tokens. "
                "Token counts are rough estimates from prompt length, not tokenizer-exact. Empty responses are excluded."
            )
        else:
            note = (
                f"No built-in pricing data for {model}. Token counts are still shown, but USD cost is unavailable. "
                "Token counts are rough estimates from prompt length, not tokenizer-exact."
            )

        if self.resume_var.get():
            note += " Resume skips are not estimated in advance."
        self.estimate_var.set(estimate_line)
        self.pricing_note_var.set(note)

    def on_show(self):
        self._apply_model_selection()
        self.refresh_estimate()

    def _append_log(self, line: str):
        self.log_text.config(state=NORMAL)
        self.log_text.insert(END, line + "\n")
        self.log_text.see(END)
        self.log_text.config(state=DISABLED)

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "log":
                    self._append_log(payload["text"])
                elif kind == "progress":
                    self.done_tasks = payload["done"]
                    self.total_tasks = payload["total"]
                    self.pbar["maximum"] = max(1, self.total_tasks)
                    self.pbar["value"] = self.done_tasks
                    self.progress_text_var.set(payload["label"])
                elif kind == "done":
                    self.running = False
                    self.start_btn.config(state=NORMAL)
                    self.stop_btn.config(state=DISABLED)
                    self.progress_text_var.set(payload["label"])
                    messagebox.showinfo("Run complete", payload["text"])
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _log(self, text: str):
        self.msg_queue.put(("log", {"text": text}))

    def _set_progress(self, done: int, total: int, label: str):
        self.msg_queue.put(("progress", {"done": done, "total": total, "label": label}))

    def stop_run(self):
        if self.running:
            self.stop_event.set()
            self._log("Stop requested. Current API call will finish before stopping.")

    def start_run(self):
        if self.running:
            return
        if not openai_available():
            messagebox.showerror("Dependency missing", "The 'openai' package is not installed. Run: pip install openai")
            return
        api_key = self.api_key_var.get().strip()
        self._apply_model_selection()
        model = self._resolved_model().strip()
        if not api_key:
            messagebox.showwarning("API key required", "Provide an OpenAI API key or set OPENAI_API_KEY.")
            return
        if not model:
            messagebox.showwarning("Model required", "Select a supported model.")
            return
        try:
            float(self.temp_var.get().strip())
        except Exception:
            messagebox.showwarning("Temperature", "Temperature must be numeric.")
            return

        self.running = True
        self.stop_event.clear()
        self.done_tasks = 0
        self.total_tasks = 0
        self.pbar["value"] = 0
        self.log_text.config(state=NORMAL)
        self.log_text.delete("1.0", END)
        self.log_text.config(state=DISABLED)
        self.start_btn.config(state=DISABLED)
        self.stop_btn.config(state=NORMAL)
        self.worker = threading.Thread(target=self._run_worker, daemon=True)
        self.worker.start()

    def _run_worker(self):
        run_dir: Path | None = None
        try:
            headers, students = build_students(
                self.state.csv_path,
                self.state.selected_first_col,
                self.state.selected_last_col,
                self.state.selected_name_col,
                self.state.selected_id_col,
                self.state.selected_response_cols,
            )
            run_dir = prepare_run_dir(
                self.state.output_folder,
                self.state.create_timestamped_subfolder,
                resume_existing=self.resume_var.get(),
            )
            run_log_path = run_dir / "logs" / "run.log"
            error_log_path = run_dir / "logs" / "errors.log"
            write_log(run_log_path, "Run started.")
            self._log(f"Run folder: {run_dir}")

            client = make_client(self.api_key_var.get().strip())
            model = self._resolved_model().strip() or DEFAULT_MODEL
            temperature = float(self.temp_var.get().strip())
            system_instruction = self.sys_text.get("1.0", END).strip() or DEFAULT_SYSTEM_INSTRUCTION
            config = {
                "timestamp": run_dir.name.replace("output_run_", ""),
                "input_csv_path": self.state.csv_path,
                "detected": {
                    "first_col": self.state.selected_first_col,
                    "last_col": self.state.selected_last_col,
                    "name_col": self.state.selected_name_col,
                    "id_col": self.state.selected_id_col,
                    "response_columns": self.state.selected_response_cols,
                    "original_headers": headers,
                },
                "question_mapping": {
                    q.question_id: {"columns": q.columns, "rubric_hash": hash_text(q.rubric), "max_points": q.max_points}
                    for q in self.state.questions
                },
                "model_settings": {"model": model, "temperature": temperature},
                "options": {
                    "resume": self.resume_var.get(),
                    "export_per_question_files": self.export_pq_var.get(),
                    "export_per_student_combined": self.export_ps_var.get(),
                },
                "app_version": APP_VERSION,
            }
            write_run_config(run_dir, config)

            total = len(students) * len(self.state.questions)
            done = 0
            self._set_progress(0, total, f"Student 0/{len(students)} - Q0/{len(self.state.questions)}")
            results_index: dict[tuple[str, str], dict] = {}

            for s_idx, student in enumerate(students, start=1):
                for q_idx, q in enumerate(self.state.questions, start=1):
                    if self.stop_event.is_set():
                        self._log("Stop acknowledged. Ending run after current completion.")
                        break
                    label = f"Student {s_idx}/{len(students)} - {q.question_id} ({q_idx}/{len(self.state.questions)})"
                    existing = load_existing_result(run_dir, q.question_id, student.safe_stem) if self.resume_var.get() else None
                    if existing:
                        results_index[(student.student_id, q.question_id)] = existing
                        done += 1
                        self._set_progress(done, total, f"{label} [resume skip]")
                        self._log(f"SKIP {student.display_name} {q.question_id} (already graded)")
                        continue

                    chunks = [student.responses_by_column.get(c, "") for c in q.columns]
                    chunks = [c for c in chunks if c.strip()]
                    response_text = "\n\n-----\n\n".join(chunks).strip()
                    if not response_text:
                        result = {
                            "student_id": student.student_id_raw or "",
                            "student_name": student.display_name,
                            "question_id": q.question_id,
                            "question_title": q.question_title,
                            "max_points": q.max_points,
                            "score_total": 0,
                            "criteria": [],
                            "feedback": "No response provided.",
                            "flags": {
                                "off_topic": False,
                                "too_short": True,
                                "suspected_plagiarism": False,
                                "policy_issue": False,
                            },
                            "raw_response_text": "",
                            "model_metadata": {"model": model, "temperature": temperature, "timestamp": ""},
                        }
                    else:
                        try:
                            result = grade_response(
                                client,
                                model=model,
                                temperature=temperature,
                                system_instruction=system_instruction,
                                rubric=q.rubric,
                                model_answer=q.model_answer,
                                extra_instructions=q.extra_instructions,
                                student_response=response_text,
                                student_id=student.student_id_raw or student.student_id,
                                student_name=student.display_name,
                                question_id=q.question_id,
                                question_title=q.question_title,
                                max_points=q.max_points,
                            )
                        except Exception as e:
                            write_log(error_log_path, f"{student.safe_stem}::{q.question_id}::{repr(e)}")
                            self._log(f"ERROR {student.display_name} {q.question_id}: {e}")
                            done += 1
                            self._set_progress(done, total, label)
                            continue

                    write_per_question_outputs(
                        run_dir=run_dir,
                        question_id=q.question_id,
                        safe_stem=student.safe_stem,
                        result=result,
                        export_txt=self.export_pq_var.get(),
                    )
                    results_index[(student.student_id, q.question_id)] = result
                    write_log(run_log_path, f"{student.safe_stem}::{q.question_id}::{result.get('score_total', '')}")
                    self._log(f"DONE {student.display_name} {q.question_id}: {result.get('score_total')} / {q.max_points}")
                    done += 1
                    self._set_progress(done, total, label)
                if self.stop_event.is_set():
                    break

            if self.export_ps_var.get():
                write_per_student_reports(run_dir, students, self.state.questions, results_index)
            export_gradebook_final(run_dir, students, self.state.questions, results_index)
            export_lms_upload_csv(run_dir, students, self.state.questions, results_index)
            export_summary_per_question(run_dir, students, self.state.questions, results_index)
            write_log(run_log_path, "Run finished.")
            text = f"Outputs written to:\n{run_dir}"
            if self.stop_event.is_set():
                self.msg_queue.put(("done", {"label": "Stopped (partial outputs exported).", "text": text}))
            else:
                self.msg_queue.put(("done", {"label": "Completed.", "text": text}))
        except Exception as e:
            if run_dir:
                try:
                    write_log(run_dir / "logs" / "errors.log", f"FATAL::{repr(e)}")
                except Exception:
                    pass
            self.msg_queue.put(("done", {"label": "Run failed.", "text": f"Error: {e}"}))


class WizardApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Essay Grading Wizard")
        self.geometry("1220x840")
        self.minsize(1120, 760)
        self.defaults, self.startup_warnings = load_app_defaults()
        self.state_data = AppState()
        self.state_data.output_folder = self.defaults.output_folder
        self.state_data.create_timestamped_subfolder = self.defaults.create_timestamped_subfolder
        self.presets_path = app_dir() / "rubric_presets.json"
        self.presets = self._load_presets()

        self.container = ttk.Frame(self)
        self.container.pack(fill=BOTH, expand=True)
        self.step1 = Step1Frame(self.container, self.state_data, self.defaults)
        self.step2 = Step2Frame(self.container, self.state_data, self.get_preset_names)
        self.step3 = Step3Frame(self.container, self.state_data, self.get_presets, self._save_presets)
        self.step4 = Step4Frame(
            self.container,
            self.state_data,
            self.get_presets,
            self.defaults,
            self.save_current_defaults,
            self.reset_to_stock_defaults,
        )
        self.steps = [self.step1, self.step2, self.step3, self.step4]
        self.step_index = 0
        for s in self.steps:
            s.pack_forget()

        nav = ttk.Frame(self)
        nav.pack(fill="x", padx=8, pady=8)
        self.back_btn = ttk.Button(nav, text="Back", command=self.go_back)
        self.back_btn.pack(side="left")
        self.next_btn = ttk.Button(nav, text="Next", command=self.go_next)
        self.next_btn.pack(side="right")
        self.step_label = ttk.Label(nav, text="")
        self.step_label.pack(side="right", padx=12)
        self._show_step(0)
        if self.startup_warnings:
            self.after(150, self._show_startup_warnings)

    def _load_presets(self) -> dict:
        if not self.presets_path.exists():
            return {}
        try:
            return json.loads(self.presets_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_presets(self, presets: dict):
        self.presets = presets
        self.presets_path.write_text(json.dumps(self.presets, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_presets(self) -> dict:
        return self.presets

    def get_preset_names(self) -> list[str]:
        return sorted(self.presets.keys())

    def _show_step(self, idx: int):
        self.step_index = idx
        for s in self.steps:
            s.pack_forget()
        self.steps[idx].pack(fill=BOTH, expand=True)
        if hasattr(self.steps[idx], "on_show"):
            self.steps[idx].on_show()
        self.back_btn.config(state=(NORMAL if idx > 0 else DISABLED))
        if idx == len(self.steps) - 1:
            self.next_btn.config(state=DISABLED, text="Next")
        else:
            self.next_btn.config(state=NORMAL, text="Next")
        self.step_label.config(text=f"Step {idx + 1}/{len(self.steps)}")

    def _show_startup_warnings(self):
        if self.startup_warnings:
            messagebox.showwarning("Config warning", "\n".join(self.startup_warnings))

    def _collect_defaults_from_ui(self) -> AppDefaults:
        model = self.step4._resolved_model().strip() or DEFAULT_MODEL
        temperature = DEFAULT_TEMPERATURE
        try:
            temperature = float(self.step4.temp_var.get().strip())
        except Exception:
            pass
        return AppDefaults(
            output_folder=self.step1.out_var.get().strip(),
            create_timestamped_subfolder=bool(self.step1.timestamp_var.get()),
            api_key=self.step4.api_key_var.get().strip(),
            model=model,
            temperature=temperature,
            resume_if_graded=bool(self.step4.resume_var.get()),
            export_per_question_files=bool(self.step4.export_pq_var.get()),
            export_per_student_combined=bool(self.step4.export_ps_var.get()),
            system_instruction=self.step4.sys_text.get("1.0", END).strip() or DEFAULT_SYSTEM_INSTRUCTION,
        )

    def _apply_defaults_to_ui(self):
        self.state_data.output_folder = self.defaults.output_folder
        self.state_data.create_timestamped_subfolder = self.defaults.create_timestamped_subfolder
        self.step1.out_var.set(self.defaults.output_folder)
        self.step1.timestamp_var.set(self.defaults.create_timestamped_subfolder)
        self.step4.api_key_var.set(self.defaults.api_key or os.getenv("OPENAI_API_KEY", ""))
        self.step4.model_choice_var.set(MODEL_ID_TO_LABEL.get(self.defaults.model, MODEL_PICKER_OPTIONS[0][1]))
        self.step4.temp_var.set(str(self.defaults.temperature))
        self.step4.resume_var.set(self.defaults.resume_if_graded)
        self.step4.export_pq_var.set(self.defaults.export_per_question_files)
        self.step4.export_ps_var.set(self.defaults.export_per_student_combined)
        self.step4.sys_text.delete("1.0", END)
        self.step4.sys_text.insert("1.0", self.defaults.system_instruction)
        self.step4._apply_model_selection()
        self.step4.refresh_estimate()

    def save_current_defaults(self):
        defaults = self._collect_defaults_from_ui()
        write_app_defaults(defaults)
        self.defaults = defaults
        self.step4.defaults = defaults
        self.step1.defaults = defaults
        messagebox.showinfo("Defaults saved", f"Saved startup defaults to:\n{app_config_path()}")

    def reset_to_stock_defaults(self):
        defaults = AppDefaults()
        write_app_defaults(defaults)
        self.defaults = defaults
        self.step4.defaults = defaults
        self.step1.defaults = defaults
        self._apply_defaults_to_ui()
        messagebox.showinfo("Defaults reset", f"Reverted startup defaults to stock values in:\n{app_config_path()}")

    def go_back(self):
        if self.step4.running:
            messagebox.showinfo("Busy", "Stop the current run before navigating.")
            return
        if self.step_index > 0:
            self._show_step(self.step_index - 1)

    def go_next(self):
        if self.step4.running:
            messagebox.showinfo("Busy", "Stop the current run before navigating.")
            return
        if self.step_index == 0 and not self.step1.validate_and_commit():
            return
        if self.step_index == 1 and not self.step2.validate_and_commit():
            return
        if self.step_index == 2 and not self.step3.validate_and_commit():
            return
        if self.step_index < len(self.steps) - 1:
            self._show_step(self.step_index + 1)


def main():
    app = WizardApp()
    app.mainloop()


if __name__ == "__main__":
    main()
