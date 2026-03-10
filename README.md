# Essay Grading Wizard

Single-entry Tkinter desktop app that ingests an LMS CSV and runs end-to-end grading in one flow.

## Run

1. Install dependencies:
   - `pip install -r requirements.txt`
2. Launch:
   - `python app.py`

Optional PyInstaller build:
- `powershell -ExecutionPolicy Bypass -File .\build_exe.ps1`
- Output: `dist\EssayChecker.exe`

## GitHub Prep

Before publishing this repo, keep generated output and secrets out of version control.

- `.gitignore` excludes build artifacts, local config, caches, and `Sample Run/`.
- `Sample Run/` currently contains grading output and an `OpenAISecretKey.txt` file, so it should not be pushed.

## Startup Defaults

If `app_config.yaml` or `app_config.yml` exists next to `app.py`, the app loads it on startup and uses it for default values in the wizard.

Supported keys:
- `output_folder`
- `create_timestamped_subfolder`
- `api_key`
- `run_defaults.model`
- `run_defaults.temperature`
- `run_defaults.resume_if_graded`
- `run_defaults.export_per_question_files`
- `run_defaults.export_per_student_combined`
- `run_defaults.system_instruction`

See `app_config.yaml.example` for the expected shape.

## Wizard Steps

1. Select CSV + Output Folder
   - Pick LMS CSV and results folder.
   - Selecting a CSV automatically loads the preview and auto-detects name columns, ID column, and response columns (`Response*`).
   - Manual overrides are hidden by default and can be opened only when needed.
   - Preview of first 10 rows.
2. Question Preview
   - Shows the auto-mapped `Q1..Qn` preview from the detected response columns.
   - Manual question tools are hidden by default and can be opened only when needed for add/remove/reorder/merge changes.
3. Rubrics & Model Answers
   - Per-question rubric/model/instructions and max points.
   - Rubric, model answer, and extra instructions can be loaded from `.txt` files to avoid copy/paste.
   - Save/load presets from `rubric_presets.json` next to `app.py`.
4. Run
   - Set API key, choose one of the supported grading models, and set temperature when allowed.
   - The global system instruction can also be loaded from a `.txt` file.
   - GPT-5-family models use their default temperature only; the app disables custom temperature for them.
   - Review the rough token and USD cost estimate before starting.
   - `Set as default settings` writes the current startup defaults to `app_config.yaml`.
   - `Revert to stock configs` overwrites `app_config.yaml` with the built-in defaults and refreshes the UI.
   - Resume mode skips already graded `(student, question)` results.
   - Live progress and logs.

## Output Folder Structure

When "Create timestamped subfolder" is enabled:

`output_run_<timestamp>/`
- `run_config.json`
- `logs/run.log`
- `logs/errors.log`
- `per_student/<SAFE_STEM>.json`
- `per_student/<SAFE_STEM>.txt`
- `per_question/<QID>/<SAFE_STEM>.json`
- `per_question/<QID>/<SAFE_STEM>.txt` (only if "Export per-question files" checked)
- `gradebook_final.csv`
- `ForLMSUpload/<QID>.csv`
- `summary_per_question.csv`

## Notes

- Resume checks `per_question/<QID>/<SAFE_STEM>.json` and skips API calls for existing items.
- Final CSV files are always regenerated from the current run state.
- `ForLMSUpload` contains one LMS import CSV per question, each with headers `name,mark,feedback`.
- Empty responses are handled without an API call and scored as 0 with `too_short=true`.
