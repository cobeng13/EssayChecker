#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CombinedApp.py
One EXE-friendly Tkinter app that bundles:
  1) Essay Checker (OpenAI grading) 
  2) Scores → CSV Compiler
Tabs via ttk.Notebook so end-users only open one program.

Build (Windows example):
    pyinstaller --onefile --noconsole CombinedApp.py
"""

import os, sys, json, time, glob, csv, threading, traceback, re
from pathlib import Path
from typing import Any, Dict, List

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter import Text, StringVar, BOTH, END, DISABLED, NORMAL
from tkinter.scrolledtext import ScrolledText

# ----------------------- Essay Checker (from EssayChecker_GUI.py, adapted to Frame) -----------------------
try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # Allow app to load without openai installed (CSV tab still usable)

MODEL = "gpt-4.1-mini"
TEMPERATURE = 0.2
MAX_RETRIES = 5

SCHEMA = {
    "name": "grading_result",
    "schema": {
        "type": "object",
        "properties": {
                "student": {
                    "type": "object",
                    "properties": {
                        "last_name": {"type": "string"},
                        "first_name": {"type": "string"},
                        "file_name": {"type": "string"}
                    },
                    "required": ["last_name", "first_name", "file_name"],
                    "additionalProperties": False
                },
                "score": {
                    "type": "object",
                    "properties": {
                        "total": {"type": "number"},
                        "max_points": {"type": "number"}
                    },
                    "required": ["total", "max_points"],
                    "additionalProperties": False
                },
                "criteria": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "max_points": {"type": "number"},
                            "points": {"type": "number"},
                            "justification": {"type": "string"}
                        },
                        "required": ["name", "max_points", "points", "justification"],
                        "additionalProperties": False
                    }
                },
                "feedback": {"type": "string"},
                "flags": {
                    "type": "object",
                    "properties": {
                        "possible_hallucinations": {"type": "boolean"},
                        "possible_plagiarism": {"type": "boolean"},
                        "off_topic": {"type": "boolean"}
                    },
                    "required": ["possible_hallucinations", "possible_plagiarism", "off_topic"],
                    "additionalProperties": False
                }
        },
        "required": ["student", "score", "criteria", "feedback", "flags"],
        "additionalProperties": False
    },
    "strict": True
}

def _safe_title(s: str) -> str:
    return s.title() if isinstance(s, str) else s

def _name_from_filename(filename: str):
    base = os.path.splitext(os.path.basename(filename))[0]
    if "_" in base and base.upper() == base:
        last, first = base.split("_", 1)
        return _safe_title(last), _safe_title(first)
    if "," in base:
        last, first = [x.strip() for x in base.split(",", 1)]
        return _safe_title(last), _safe_title(first)
    parts = base.split()
    if len(parts) >= 2:
        first = parts[0]
        last = " ".join(parts[1:])
        return _safe_title(last), _safe_title(first)
    return _safe_title(base), ""

def _to_human_txt(data: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"{data['student']['last_name']}, {data['student']['first_name']}")
    lines.append(f"Score: {data['score']['total']} / {data['score']['max_points']}")
    lines.append("")
    lines.append("Criteria:")
    for c in data["criteria"]:
        lines.append(f"- {c['name']}: {c['points']} / {c['max_points']}")
        lines.append(f"  Why: {c['justification']}")
    lines.append("")
    lines.append("Feedback:")
    lines.append(data["feedback"])
    lines.append("")
    lines.append("Flags:")
    lines.append(f"- Off topic: {data['flags']['off_topic']}")
    lines.append(f"- Possible plagiarism: {data['flags']['possible_plagiarism']}")
    lines.append(f"- Possible hallucinations: {data['flags']['possible_hallucinations']}")
    lines.append("----")
    return "\n".join(lines)

def _ensure_dirs(path: str):
    os.makedirs(path, exist_ok=True)
    os.makedirs(os.path.join(path, "logs"), exist_ok=True)

def _append_summary(summary_csv: str, row: Dict[str, Any]):
    fieldnames = ["file", "last_name", "first_name", "score", "max_points",
                  "flags_off_topic", "flags_plagiarism", "flags_hallucinations"]
    exists = os.path.exists(summary_csv)
    with open(summary_csv, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow(row)

def _call_openai(client, system_prompt: str, rubric: str,
                model_answer: str, student_text: str, student_file: str) -> Dict[str, Any]:
    last, first = _name_from_filename(student_file)
    user_content = (
        "You will grade the student's essay strictly using the rubric and style guidelines below.\n\n"
        "=== RUBRIC ===\n" + rubric + "\n\n"
        "=== MODEL ANSWER ===\n" + model_answer + "\n\n"
        "=== STUDENT ESSAY ===\n" + student_text + "\n\n"
        "Return ONLY the structured object as per the JSON Schema."
    )
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                temperature=TEMPERATURE,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": SCHEMA["name"],
                        "schema": SCHEMA["schema"],
                        "strict": True
                    }
                }
            )
            data = json.loads(resp.choices[0].message.content)
            data["student"]["last_name"] = last
            data["student"]["first_name"] = first
            data["student"]["file_name"] = os.path.basename(student_file)
            return data
        except Exception as e:
            if attempt == MAX_RETRIES:
                raise
            time.sleep(min(2 ** (attempt - 1), 30))

class _GraderWorker(threading.Thread):
    def __init__(self, gui_ref):
        super().__init__(daemon=True)
        self.gui = gui_ref
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        try:
            self._grade_all()
        except Exception as e:
            self.gui.log(f"❌ Fatal error: {e}")
            self.gui.log(traceback.format_exc())
        finally:
            self.gui.on_done()

    def _grade_all(self):
        sys_prompt = self.gui.general_instruction.get("1.0", END).strip()
        rubric = self.gui.rubric_text.get("1.0", END).strip()
        model_ans = self.gui.model_answer.get("1.0", END).strip()
        api_key = self.gui.api_key_var.get().strip() or os.getenv("OPENAI_API_KEY", "")

        if OpenAI is None:
            self.gui.log("❌ The 'openai' package is not installed. Install with: pip install openai")
            return

        if not api_key:
            self.gui.log("❌ No API key provided. Enter it or set OPENAI_API_KEY.")
            return
        if not sys_prompt or not rubric or not model_ans:
            self.gui.log("❌ Please fill in General Instruction, Rubric, and Model Answer.")
            return

        input_dir = self.gui.input_dir_var.get().strip()
        output_dir = self.gui.output_dir_var.get().strip()
        if not input_dir or not os.path.isdir(input_dir):
            self.gui.log("❌ Invalid input folder.")
            return
        if not output_dir:
            self.gui.log("❌ Please choose an output folder.")
            return
        _ensure_dirs(output_dir)
        summary_csv = os.path.join(output_dir, "_summary.csv")
        log_dir = os.path.join(output_dir, "logs")

        client = OpenAI(api_key=api_key)

        student_files = sorted(glob.glob(os.path.join(input_dir, "*.txt")))
        if not student_files:
            self.gui.log("No input .txt files found.")
            return

        self.gui.set_progress_total(len(student_files))
        for f in student_files:
            if self._stop_event.is_set():
                self.gui.log("⏹ Stopped by user.")
                break

            base = os.path.basename(f)
            last, first = _name_from_filename(f)
            stem = f"{last}_{first}".replace(" ", "")
            json_out = os.path.join(output_dir, stem + ".json")
            txt_out = os.path.join(output_dir, stem + ".txt")

            self.gui.log(f"📝 Grading: {base}")
            try:
                with open(f, "r", encoding="utf-8") as inf:
                    student_text = inf.read().strip()
                data = _call_openai(client, sys_prompt, rubric, model_ans, student_text, f)

                with open(json_out, "w", encoding="utf-8") as jf:
                    json.dump(data, jf, ensure_ascii=False, indent=2)

                human = _to_human_txt(data)
                with open(txt_out, "w", encoding="utf-8") as tf:
                    tf.write(human)

                row = {
                    "file": os.path.basename(f),
                    "last_name": data["student"]["last_name"],
                    "first_name": data["student"]["first_name"],
                    "score": data["score"]["total"],
                    "max_points": data["score"]["max_points"],
                    "flags_off_topic": data["flags"]["off_topic"],
                    "flags_plagiarism": data["flags"]["possible_plagiarism"],
                    "flags_hallucinations": data["flags"]["possible_hallucinations"]
                }
                _append_summary(summary_csv, row)
                self.gui.log(f"   ✅ Done: {stem} — {data['score']['total']} / {data['score']['max_points']}")
            except Exception as e:
                try:
                    with open(os.path.join(log_dir, "errors.log"), "a", encoding="utf-8") as logf:
                        logf.write(f"{base} :: {repr(e)}\n")
                except Exception:
                    pass
                self.gui.log(f"   ❌ Error on '{base}': {e}")
            finally:
                self.gui.step_progress()

class EssayCheckerFrame(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.api_key_var = StringVar(value=os.getenv("OPENAI_API_KEY", ""))
        self.input_dir_var = StringVar(value="")
        self.output_dir_var = StringVar(value="output")
        self._build_ui()

        self.worker: _GraderWorker | None = None
        self.progress_total = 0
        self.progress_var = StringVar(value="0 / 0")

    def _build_ui(self):
        # Top fields
        top = ttk.Frame(self); top.pack(fill=BOTH, expand=True, padx=6, pady=6)
        ttk.Label(top, text="General Instruction (system prompt)").grid(row=0, column=0, sticky="w")
        self.general_instruction = Text(top, height=6, wrap="word")
        self.general_instruction.grid(row=1, column=0, padx=6, pady=2, sticky="nsew")

        ttk.Label(top, text="Rubric").grid(row=0, column=1, sticky="w")
        self.rubric_text = Text(top, height=6, wrap="word")
        self.rubric_text.grid(row=1, column=1, padx=6, pady=2, sticky="nsew")

        ttk.Label(top, text="Model Answer").grid(row=0, column=2, sticky="w")
        self.model_answer = Text(top, height=6, wrap="word")
        self.model_answer.grid(row=1, column=2, padx=6, pady=2, sticky="nsew")

        ttk.Label(top, text="OpenAI API Key").grid(row=2, column=0, sticky="w", padx=2, pady=(8,2))
        self.api_key_entry = ttk.Entry(top, textvariable=self.api_key_var, show="•", width=40)
        self.api_key_entry.grid(row=2, column=1, sticky="w", padx=2, pady=(8,2))
        ttk.Button(top, text="Show/Hide", command=self._toggle_api_visibility).grid(row=2, column=2, sticky="w", padx=2, pady=(8,2))

        top.grid_columnconfigure(0, weight=1)
        top.grid_columnconfigure(1, weight=1)
        top.grid_columnconfigure(2, weight=1)
        top.grid_rowconfigure(1, weight=1)

        # Dirs
        dirs = ttk.Frame(self); dirs.pack(fill="x", padx=6, pady=6)
        ttk.Label(dirs, text="Input folder (student .txt files):").grid(row=0, column=0, sticky="w")
        ttk.Entry(dirs, textvariable=self.input_dir_var, width=60).grid(row=0, column=1, padx=4, sticky="w")
        ttk.Button(dirs, text="Browse", command=self._browse_input).grid(row=0, column=2, padx=4)

        ttk.Label(dirs, text="Output folder:").grid(row=1, column=0, sticky="w")
        ttk.Entry(dirs, textvariable=self.output_dir_var, width=60).grid(row=1, column=1, padx=4, sticky="w")
        ttk.Button(dirs, text="Browse", command=self._browse_output).grid(row=1, column=2, padx=4)

        # Controls
        controls = ttk.Frame(self); controls.pack(fill="x", padx=6, pady=6)
        self.start_btn = ttk.Button(controls, text="Start Grading", command=self.start_grading)
        self.start_btn.grid(row=0, column=0, padx=6)
        self.stop_btn = ttk.Button(controls, text="Stop", command=self.stop_grading, state=DISABLED)
        self.stop_btn.grid(row=0, column=1, padx=6)
        self.pbar = ttk.Progressbar(controls, length=250, mode="determinate")
        self.pbar.grid(row=0, column=2, padx=6, pady=6, sticky="w")
        ttk.Label(controls, textvariable=getattr(self, "progress_var", StringVar(value="0 / 0"))).grid(row=0, column=3, padx=6, sticky="w")
        self.controls_frame = controls  # compatibility

        # Log
        log_frame = ttk.Frame(self); log_frame.pack(fill=BOTH, expand=True, padx=6, pady=6)
        ttk.Label(log_frame, text="Log").pack(anchor="w")
        self.log_text = ScrolledText(log_frame, height=12, wrap="word", state=DISABLED)
        self.log_text.pack(fill=BOTH, expand=True)

    # ----- UI helpers -----
    def _toggle_api_visibility(self):
        current = self.api_key_entry.cget("show")
        self.api_key_entry.config(show="" if current else "•")

    def _browse_input(self):
        path = filedialog.askdirectory(title="Select input folder (with .txt files)")
        if path:
            self.input_dir_var.set(path)

    def _browse_output(self):
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            self.output_dir_var.set(path)

    # ----- Controls -----
    def start_grading(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "Grading already in progress.")
            return
        self.set_progress_total(0)
        self.clear_log()
        self.start_btn.config(state=DISABLED)
        self.stop_btn.config(state=NORMAL)
        self.worker = _GraderWorker(self)
        self.worker.start()
        self.log("🚀 Started. Outputs will go to your chosen folder.")

    def stop_grading(self):
        if self.worker and self.worker.is_alive():
            self.worker.stop()
            self.log("Stopping...")

    def on_done(self):
        self.start_btn.config(state=NORMAL)
        self.stop_btn.config(state=DISABLED)
        self.log("✅ Finished.")

    # ----- Logging/Progress -----
    def log(self, msg: str):
        self.log_text.config(state=NORMAL)
        self.log_text.insert(END, msg + "\n")
        self.log_text.see(END)
        self.log_text.config(state=DISABLED)

    def clear_log(self):
        self.log_text.config(state=NORMAL)
        self.log_text.delete("1.0", END)
        self.log_text.config(state=DISABLED)

    def set_progress_total(self, total: int):
        self.progress_total = max(0, total)
        self.pbar["maximum"] = self.progress_total if self.progress_total > 0 else 1
        self.pbar["value"] = 0
        if hasattr(self, "progress_var"):
            self.progress_var.set(f"0 / {self.progress_total}")
        else:
            self.progress_var = StringVar(value=f"0 / {self.progress_total}")

    def step_progress(self):
        self.pbar["value"] = min(self.pbar["value"] + 1, self.progress_total)
        if hasattr(self, "progress_var"):
            self.progress_var.set(f"{int(self.pbar['value'])} / {self.progress_total}")

# ----------------------- CSV Compiler (from Grade Compiler.py, adapted to Frame) -----------------------

DEFAULT_CSV_NAME = "Compiled_Scores_Feedback.csv"
_INVALID_FILENAME_CHARS = set('<>:"/\\|?*')

def _safe_read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        try:
            return p.read_text(encoding="latin-1", errors="ignore")
        except Exception:
            return ""

def _parse_score_from_txt(text: str):
    m = re.search(r"Score\s*[:=]?\s*(\d+)\s*/", text, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None

def _extract_feedback_block_from_txt(text: str):
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^\s*Feedback\s*:\s*$", line, re.IGNORECASE):
            start = i + 1
            break
    if start is None:
        return None
    collected = []
    for j in range(start, len(lines)):
        line = lines[j]
        if re.match(r"^\s*Flags\s*:", line, re.IGNORECASE):
            break
        if re.match(r"^\s*-{3,}\s*$", line):
            break
        collected.append(line.rstrip())
    feedback = "\n".join(collected).strip()
    return feedback or None

def _normalize_spaces(s: str):
    return re.sub(r"[ \t]+", " ", s).strip()

def _combine_feedback(*chunks):
    seen = set()
    parts = []
    for chunk in chunks:
        if not chunk:
            continue
        bits = re.split(r"(?<=[.!?])\s+|\n+", chunk.strip())
        for b in bits:
            b_norm = _normalize_spaces(b)
            if b_norm and b_norm.lower() not in seen:
                seen.add(b_norm.lower())
                parts.append(b_norm)
    return " ".join(parts).strip()

def _name_from_stem(stem: str, style: str):
    raw = stem.replace("-", " ").replace("_", " ").strip()
    if style == "plain":
        return raw
    parts = [p for p in raw.split() if p]
    if len(parts) >= 2:
        last = parts[0].upper()
        first = parts[1].capitalize()
        middle = " ".join(p.capitalize() for p in parts[2:]) if len(parts) > 2 else ""
        return f"{last}, {first}{(' ' + middle) if middle else ''}"
    return raw

def _discover_files(folder: Path, include_json: bool):
    by_stem = {}
    for p in folder.iterdir():
        if not p.is_file():
            continue
        suf = p.suffix.lower()
        if suf == ".txt" or (include_json and suf == ".json"):
            stem = p.stem
            d = by_stem.setdefault(stem, {"txt": None, "json": None})
            if suf == ".txt":
                d["txt"] = p
            elif suf == ".json":
                d["json"] = p
    return by_stem

def _validate_filename(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("Filename cannot be empty.")
    if any(c in _INVALID_FILENAME_CHARS for c in name):
        raise ValueError('Filename cannot contain any of the following characters: <>:"/\\|?*')
    if not name.lower().endswith(".csv"):
        name += ".csv"
    return name

def _process_folder(folder: Path, out_dir: Path, csv_name: str,
                   name_style: str, include_json: bool, logfn):
    mapping = _discover_files(folder, include_json=include_json)
    stems = sorted(mapping.keys())
    if not stems:
        raise RuntimeError("No matching .txt files were found in the selected folder.")

    rows = []
    for i, stem in enumerate(stems, 1):
        files = mapping[stem]
        score = None
        fb_json = None
        fb_txt = None

        if files["json"] and files["json"].exists():
            try:
                data = json.loads(files["json"].read_text(encoding="utf-8"))
                score = data.get("score", {}).get("total", None)
                fb_json = data.get("feedback", None)
            except Exception as e:
                logfn(f"[{i}/{len(stems)}] {files['json'].name}: JSON read error: {e}")

        if files["txt"] and files["txt"].exists():
            txt = _safe_read_text(files["txt"])
            if score is None:
                score = _parse_score_from_txt(txt)
            fb_txt = _extract_feedback_block_from_txt(txt)

        feedback = _combine_feedback(fb_json, fb_txt)
        if not feedback and files["txt"]:
            txt2 = _safe_read_text(files["txt"])
            crit_start = txt2.lower().find("criteria:")
            if crit_start != -1:
                feedback = _normalize_spaces(txt2[crit_start:crit_start+1200])

        rows.append({
            "Names": _name_from_stem(stem, name_style),
            "Score": score if isinstance(score, int) else "",
            "Feedback": feedback if isinstance(feedback, str) else ""
        })
        logfn(f"[{i}/{len(stems)}] Processed: {stem}")

    csv_path = out_dir / csv_name
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["Names", "Score", "Feedback"])
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    return csv_path, len(rows)

class CompilerFrame(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.in_folder = tk.StringVar(value="")
        self.out_folder = tk.StringVar(value=os.getcwd())
        self.out_filename = tk.StringVar(value=DEFAULT_CSV_NAME)
        self.name_style = tk.StringVar(value="last_first_middle")
        self.include_json = tk.BooleanVar(value=True)
        self._build_ui()

    def _build_ui(self):
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)

        row1 = ttk.Frame(frm); row1.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(row1, text="Input folder (contains graded .txt files; .json optional):").pack(anchor="w")
        row1b = ttk.Frame(row1); row1b.pack(fill=tk.X, pady=4)
        ttk.Entry(row1b, textvariable=self.in_folder).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row1b, text="Select…", command=self._select_in_folder).pack(side=tk.LEFT, padx=(8, 0))

        row_out = ttk.Frame(frm); row_out.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(row_out, text="Output folder (where to save the CSV):").pack(anchor="w")
        row_outb = ttk.Frame(row_out); row_outb.pack(fill=tk.X, pady=4)
        ttk.Entry(row_outb, textvariable=self.out_folder).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row_outb, text="Select…", command=self._select_out_folder).pack(side=tk.LEFT, padx=(8, 0))

        row_name = ttk.Frame(frm); row_name.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(row_name, text="CSV filename:").pack(side=tk.LEFT)
        ttk.Entry(row_name, textvariable=self.out_filename, width=40).pack(side=tk.LEFT, padx=8)

        row2 = ttk.Frame(frm); row2.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(row2, text="Name format:").pack(side=tk.LEFT)
        ttk.Radiobutton(row2, text="LAST, First Middle", value="last_first_middle",
                        variable=self.name_style).pack(side=tk.LEFT, padx=8)
        ttk.Radiobutton(row2, text="Plain (from filename)", value="plain",
                        variable=self.name_style).pack(side=tk.LEFT, padx=8)
        ttk.Checkbutton(row2, text="Include JSON (merge score/feedback if present)",
                        variable=self.include_json).pack(side=tk.RIGHT)

        row3 = ttk.Frame(frm); row3.pack(fill=tk.X, pady=(0, 8))
        self.btn_compile = ttk.Button(row3, text="Compile → CSV", command=self._run_compile)
        self.btn_compile.pack(side=tk.LEFT)
        ttk.Button(row3, text="Open Output Folder", command=self._open_out_folder).pack(side=tk.LEFT, padx=8)

        ttk.Label(frm, text="Log:").pack(anchor="w")
        self.log = ScrolledText(frm, height=18, wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=True)

        self.status = tk.StringVar(value="Ready.")
        ttk.Label(frm, textvariable=self.status, relief=tk.SUNKEN, anchor="w").pack(fill=tk.X, pady=(8, 0))

    def _select_in_folder(self):
        chosen = filedialog.askdirectory(title="Select input folder with .txt (and optional .json)")
        if chosen:
            self.in_folder.set(chosen)

    def _select_out_folder(self):
        chosen = filedialog.askdirectory(title="Select output folder for CSV")
        if chosen:
            self.out_folder.set(chosen)

    def _open_out_folder(self):
        out_dir = Path(self.out_folder.get().strip() or os.getcwd())
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(out_dir))
            elif sys.platform == "darwin":
                os.system(f'open "{out_dir}"')
            else:
                os.system(f'xdg-open "{out_dir}"')
        except Exception as e:
            messagebox.showerror("Open Folder", f"Could not open folder:\n{e}")

    def _log(self, text: str):
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.update_idletasks()

    def _run_compile(self):
        in_dir = Path(self.in_folder.get().strip())
        if not in_dir.exists() or not in_dir.is_dir():
            messagebox.showerror("Input folder", "Please select a valid input folder containing .txt files.")
            return

        out_dir = Path(self.out_folder.get().strip() or os.getcwd())
        if not out_dir.exists():
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                messagebox.showerror("Output folder", f"Cannot create output folder:\n{e}")
                return

        try:
            csv_name = _validate_filename(self.out_filename.get())
        except ValueError as ve:
            messagebox.showerror("CSV filename", str(ve))
            return

        csv_path = out_dir / csv_name
        if csv_path.exists():
            if not messagebox.askyesno("Overwrite?",
                                       f"'{csv_path.name}' already exists in:\n{out_dir}\n\nOverwrite?"):
                return

        self.status.set("Compiling…")
        self._log(f"Input folder:  {in_dir}")
        self._log(f"Output folder: {out_dir}")
        self._log(f"CSV filename:  {csv_name}")
        self.btn_compile.config(state="disabled")
        self.update_idletasks()

        try:
            csv_path, count = _process_folder(
                folder=in_dir,
                out_dir=out_dir,
                csv_name=csv_name,
                name_style=self.name_style.get(),
                include_json=self.include_json.get(),
                logfn=self._log
            )
        except Exception as e:
            self._log(f"Error: {e}")
            messagebox.showerror("Error", str(e))
            self.status.set("Error.")
            self.btn_compile.config(state="normal")
            return

        self._log(f"Done. Wrote {count} rows to '{csv_path.name}'.")
        self.status.set(f"Success: {csv_path}")
        messagebox.showinfo("Success", f"Compiled {count} records.\n\nSaved to:\n{csv_path}")
        self.btn_compile.config(state="normal")

# ----------------------- App Shell with Notebook -----------------------

def main():
    root = tk.Tk()
    root.title("Essay Checker + Grade Compiler")
    root.geometry("1200x760")

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)

    essay_tab = EssayCheckerFrame(nb)
    compiler_tab = CompilerFrame(nb)

    nb.add(essay_tab, text="Essay Checker")
    nb.add(compiler_tab, text="CSV Compiler")

    root.mainloop()

if __name__ == "__main__":
    # Tkinter constants used above
    END = tk.END
    DISABLED = tk.DISABLED
    NORMAL = tk.NORMAL
    BOTH = tk.BOTH

    main()
