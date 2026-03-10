import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from utils import (
    clean,
    compose_display_name,
    detect_id_column,
    detect_name_columns,
    detect_response_columns,
    ensure_unique_stems,
    normalize_key,
    sanitize_filename,
)


@dataclass
class CsvPreview:
    headers: list[str]
    rows: list[dict[str, str]]
    first_col: str | None
    last_col: str | None
    name_col: str | None
    id_col: str | None
    response_cols: list[str]


@dataclass
class StudentRecord:
    row_index: int
    student_id: str
    student_id_raw: str
    display_name: str
    safe_stem: str
    responses_by_column: dict[str, str]
    raw_row: dict[str, str]


def _read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    encodings = ["utf-8-sig", "utf-8", "latin-1"]
    last_error: Exception | None = None
    for enc in encodings:
        try:
            with path.open("r", encoding=enc, newline="") as f:
                reader = csv.DictReader(f)
                headers = reader.fieldnames or []
                rows = [dict(r) for r in reader]
                return [h.strip() for h in headers], rows
        except Exception as e:
            last_error = e
    raise RuntimeError(f"Could not read CSV: {last_error}")


def load_preview(csv_path: str, max_rows: int = 10) -> CsvPreview:
    path = Path(csv_path)
    headers, rows = _read_csv_rows(path)
    if not headers:
        raise ValueError("CSV has no header row.")
    first_col, last_col, name_col = detect_name_columns(headers)
    id_col = detect_id_column(headers)
    response_cols = detect_response_columns(headers)
    return CsvPreview(
        headers=headers,
        rows=rows[:max_rows],
        first_col=first_col,
        last_col=last_col,
        name_col=name_col,
        id_col=id_col,
        response_cols=response_cols,
    )


def _derive_student_id(
    row: dict[str, str],
    id_col: str | None,
    first_col: str | None,
    last_col: str | None,
    name_col: str | None,
) -> tuple[str, str]:
    raw_id = clean(row.get(id_col, "")) if id_col else ""
    if raw_id:
        return normalize_key(raw_id) or raw_id, raw_id
    display = compose_display_name(row, first_col, last_col, name_col)
    fallback = normalize_key(display) or f"student_{abs(hash(display)) % 1000000}"
    return fallback, ""


def build_students(
    csv_path: str,
    first_col: str | None,
    last_col: str | None,
    name_col: str | None,
    id_col: str | None,
    response_columns: list[str],
) -> tuple[list[str], list[StudentRecord]]:
    headers, rows = _read_csv_rows(Path(csv_path))
    if not headers:
        raise ValueError("CSV has no header row.")
    missing = [c for c in response_columns if c not in headers]
    if missing:
        raise ValueError(f"Response column(s) not in CSV: {missing}")

    students: list[StudentRecord] = []
    tentative_stems: list[str] = []

    for idx, row in enumerate(rows, start=1):
        row_clean: dict[str, str] = {k: clean(v) for k, v in row.items()}
        sid_norm, sid_raw = _derive_student_id(row_clean, id_col, first_col, last_col, name_col)
        display = compose_display_name(row_clean, first_col, last_col, name_col)
        stem_seed = f"{sid_norm}_{display}" if sid_raw else display
        tentative_stems.append(sanitize_filename(stem_seed))

        responses = {c: clean(row_clean.get(c, "")) for c in response_columns}
        students.append(
            StudentRecord(
                row_index=idx,
                student_id=sid_norm,
                student_id_raw=sid_raw,
                display_name=display,
                safe_stem="",
                responses_by_column=responses,
                raw_row=row_clean,
            )
        )

    unique_stems = ensure_unique_stems(tentative_stems)
    for i, stem in enumerate(unique_stems):
        students[i].safe_stem = stem

    return headers, students


def preview_rows_for_tree(preview: CsvPreview) -> list[list[Any]]:
    out: list[list[Any]] = []
    for row in preview.rows:
        out.append([row.get(h, "") for h in preview.headers])
    return out

