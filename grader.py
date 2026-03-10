import json
import math
import time
from datetime import datetime, timezone
from typing import Any

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_TEMPERATURE = 0.0
MAX_RETRIES = 5

DEFAULT_SYSTEM_INSTRUCTION = (
    "You are a strict but fair grading assistant. Grade only against the supplied rubric. "
    "Do not invent evidence. Justify each criterion score briefly and clearly."
)

MODEL_PRICING_USD_PER_1M: dict[str, dict[str, float]] = {
    "gpt-5-mini": {"input": 0.25, "output": 2.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
}

DEFAULT_ONLY_TEMPERATURE_MODEL_PREFIXES = (
    "gpt-5",
    "gpt-5.",
)


def model_supports_custom_temperature(model: str) -> bool:
    normalized = (model or "").strip().lower()
    if not normalized:
        return True
    return not any(normalized.startswith(prefix) for prefix in DEFAULT_ONLY_TEMPERATURE_MODEL_PREFIXES)


def build_user_content(
    *,
    rubric: str,
    model_answer: str,
    extra_instructions: str,
    student_response: str,
    question_id: str,
    question_title: str,
    max_points: float,
) -> str:
    return (
        "Grade this single response strictly using the provided rubric.\n\n"
        f"Question ID: {question_id}\n"
        f"Question title: {question_title or ''}\n"
        f"Max points: {max_points}\n\n"
        "=== RUBRIC ===\n"
        f"{rubric}\n\n"
        "=== MODEL ANSWER (optional reference) ===\n"
        f"{model_answer or '(none)'}\n\n"
        "=== EXTRA INSTRUCTIONS ===\n"
        f"{extra_instructions or '(none)'}\n\n"
        "=== STUDENT RESPONSE ===\n"
        f"{student_response}\n\n"
        "Return ONLY a JSON object matching the schema."
    )


def estimate_text_tokens(text: str) -> int:
    return max(1, math.ceil(len(text or "") / 4))


def estimate_criteria_count(rubric: str) -> int:
    lines = [line.strip() for line in (rubric or "").splitlines() if line.strip()]
    bullet_like = sum(
        1
        for line in lines
        if line.startswith(("-", "*"))
        or (len(line) > 1 and line[0].isdigit() and line[1] in (".", ")"))
    )
    if bullet_like:
        return min(max(bullet_like, 1), 8)
    return min(max(len(lines) // 3, 1), 5) if lines else 3


def estimate_output_tokens(rubric: str) -> int:
    criteria_count = estimate_criteria_count(rubric)
    return 90 + (criteria_count * 45)


def estimate_call_tokens(
    *,
    system_instruction: str,
    rubric: str,
    model_answer: str,
    extra_instructions: str,
    student_response: str,
    question_id: str,
    question_title: str,
    max_points: float,
) -> tuple[int, int]:
    user_content = build_user_content(
        rubric=rubric,
        model_answer=model_answer,
        extra_instructions=extra_instructions,
        student_response=student_response,
        question_id=question_id,
        question_title=question_title,
        max_points=max_points,
    )
    input_tokens = estimate_text_tokens(system_instruction) + estimate_text_tokens(user_content)
    output_tokens = estimate_output_tokens(rubric)
    return input_tokens, output_tokens


def openai_available() -> bool:
    return OpenAI is not None


def make_client(api_key: str):
    if OpenAI is None:
        raise RuntimeError("The openai package is not installed. Install with: pip install openai")
    return OpenAI(api_key=api_key)


def _schema(max_points: float) -> dict[str, Any]:
    return {
        "name": "question_grading_result",
        "schema": {
            "type": "object",
            "properties": {
                "score_total": {"type": "number", "minimum": 0, "maximum": max_points},
                "criteria": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "points_awarded": {"type": "number"},
                            "justification": {"type": "string"},
                        },
                        "required": ["name", "points_awarded", "justification"],
                        "additionalProperties": False,
                    },
                },
                "feedback": {"type": "string"},
                "flags": {
                    "type": "object",
                    "properties": {
                        "off_topic": {"type": "boolean"},
                        "too_short": {"type": "boolean"},
                        "suspected_plagiarism": {"type": "boolean"},
                        "policy_issue": {"type": "boolean"},
                    },
                    "required": ["off_topic", "too_short", "suspected_plagiarism", "policy_issue"],
                    "additionalProperties": False,
                },
            },
            "required": ["score_total", "criteria", "feedback", "flags"],
            "additionalProperties": False,
        },
        "strict": True,
    }


def grade_response(
    client: Any,
    *,
    model: str,
    temperature: float,
    system_instruction: str,
    rubric: str,
    model_answer: str,
    extra_instructions: str,
    student_response: str,
    student_id: str,
    student_name: str,
    question_id: str,
    question_title: str,
    max_points: float,
) -> dict[str, Any]:
    if not rubric.strip():
        raise ValueError(f"Question {question_id} is missing rubric text.")

    user_content = build_user_content(
        rubric=rubric,
        model_answer=model_answer,
        extra_instructions=extra_instructions,
        student_response=student_response,
        question_id=question_id,
        question_title=question_title,
        max_points=max_points,
    )

    schema = _schema(max_points=max_points)
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            request_kwargs = {
                "model": model,
                "messages": [{"role": "system", "content": system_instruction}, {"role": "user", "content": user_content}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema["name"],
                        "schema": schema["schema"],
                        "strict": True,
                    },
                },
            }
            if model_supports_custom_temperature(model):
                request_kwargs["temperature"] = temperature

            resp = client.chat.completions.create(
                **request_kwargs
            )
            data = json.loads(resp.choices[0].message.content)
            score_total = float(data.get("score_total", 0))
            score_total = max(0.0, min(score_total, float(max_points)))
            data["score_total"] = score_total
            return {
                "student_id": student_id,
                "student_name": student_name,
                "question_id": question_id,
                "question_title": question_title,
                "max_points": float(max_points),
                "score_total": score_total,
                "criteria": data.get("criteria", []),
                "feedback": data.get("feedback", ""),
                "flags": data.get("flags", {}),
                "raw_response_text": student_response,
                "model_metadata": {
                    "model": model,
                    "temperature": temperature,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }
        except Exception as e:
            last_error = e
            if attempt == MAX_RETRIES:
                break
            time.sleep(min(2 ** (attempt - 1), 30))

    raise RuntimeError(f"OpenAI grading failed after {MAX_RETRIES} attempts: {last_error}")
