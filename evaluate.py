from __future__ import annotations

import argparse
import json
import math
import unicodedata
from pathlib import Path
from typing import Any


def _normalize_string(value: Any) -> str:
    text = unicodedata.normalize("NFC", str(value)).lower()
    return " ".join("".join(char if char.isalnum() or char.isspace() else " " for char in text).split())


def _number_equal(left: Any, right: Any) -> bool:
    if not isinstance(left, (int, float)) or isinstance(left, bool):
        return False
    if not isinstance(right, (int, float)) or isinstance(right, bool):
        return False
    if not math.isfinite(float(left)) or not math.isfinite(float(right)):
        return False
    return float(left) == float(right)


def _matches(prediction: dict[str, Any], reference: dict[str, Any]) -> bool:
    if prediction.get("type") != reference.get("type"):
        return False
    kind = reference.get("type")
    answer = prediction.get("answer")
    expected = reference.get("answer")
    if kind in {"number", "percent"}:
        if kind == "percent" and isinstance(answer, (int, float)) and not isinstance(answer, bool):
            return _number_equal(round(float(answer), 2), expected)
        return _number_equal(answer, expected)
    if kind == "string":
        forms = [expected, *(reference.get("alias") or [])]
        normalized = _normalize_string(answer) if isinstance(answer, str) else None
        return normalized is not None and any(normalized == _normalize_string(form) for form in forms)
    if kind == "date":
        return answer == expected
    if kind == "boolean":
        return type(answer) is bool and answer is expected
    if kind == "list":
        if not isinstance(answer, list) or not isinstance(expected, list) or len(answer) != len(expected):
            return False
        aliases = reference.get("alias") or []
        for index, (got, want) in enumerate(zip(answer, expected)):
            if isinstance(want, (int, float)) and not isinstance(want, bool):
                if not _number_equal(got, want):
                    return False
            else:
                forms = [want]
                if index < len(aliases) and isinstance(aliases[index], list):
                    forms.extend(aliases[index])
                if not isinstance(got, str) or not any(
                    _normalize_string(got) == _normalize_string(form) for form in forms
                ):
                    return False
        return True
    if kind == "unanswerable":
        return True
    if kind == "table":
        if not isinstance(answer, dict) or not isinstance(expected, dict):
            return False
        answer_columns = answer.get("columns")
        expected_columns = expected.get("columns")
        answer_rows = answer.get("rows")
        expected_rows = expected.get("rows")
        if not all(isinstance(value, list) for value in (answer_columns, expected_columns, answer_rows, expected_rows)):
            return False
        if len(answer_columns) != len(expected_columns) or len(answer_rows) != len(expected_rows):
            return False
        if [_normalize_string(value) for value in answer_columns] != [
            _normalize_string(value) for value in expected_columns
        ]:
            return False
        def row_matches(got: list[Any], want: list[Any]) -> bool:
            if len(got) != len(want):
                return False
            for got_cell, want_cell in zip(got, want):
                if isinstance(want_cell, (int, float)) and not isinstance(want_cell, bool):
                    if not _number_equal(got_cell, want_cell):
                        return False
                elif _normalize_string(got_cell) != _normalize_string(want_cell):
                    return False
            return True
        unmatched = list(expected_rows)
        for got_row in answer_rows:
            match = next((index for index, row in enumerate(unmatched) if row_matches(got_row, row)), None)
            if match is None:
                return False
            unmatched.pop(match)
        return not unmatched
    return False


def evaluate(reference_path: Path, submission_path: Path) -> tuple[float, dict[str, int]]:
    references = {
        row["id"]: row
        for row in (json.loads(line) for line in reference_path.open(encoding="utf-8"))
        if isinstance(row, dict) and "id" in row
    }
    predictions = {
        row["id"]: row
        for row in (json.loads(line) for line in submission_path.open(encoding="utf-8"))
        if isinstance(row, dict) and "id" in row
    }
    scores = {"correct": 0, "wrong": 0, "abstain": 0}
    for identifier, reference in references.items():
        prediction = predictions.get(identifier)
        if prediction is None or prediction.get("type") == "abstain":
            scores["abstain"] += 1
        elif _matches(prediction, reference):
            scores["correct"] += 1
        else:
            scores["wrong"] += 1
    pa = (scores["correct"] - scores["wrong"]) / len(references) if references else 0.0
    return pa, scores


def main() -> int:
    parser = argparse.ArgumentParser(description="Calculate DataMate Penalized Accuracy (PA).")
    parser.add_argument("references", type=Path)
    parser.add_argument("submission", type=Path)
    args = parser.parse_args()
    pa, scores = evaluate(args.references, args.submission)
    print(f"PA: {pa:.6f}")
    print(f"correct={scores['correct']} wrong={scores['wrong']} abstain={scores['abstain']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
