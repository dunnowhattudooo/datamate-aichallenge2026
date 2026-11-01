from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from .engine import DataMateAgent


def _business_path(data_root: Path, business_id: str) -> Path:
    direct = data_root / business_id
    if direct.is_dir():
        return direct
    nested = data_root / "data" / business_id
    if nested.is_dir():
        return nested
    if (data_root / "README.md").is_file():
        return data_root
    raise FileNotFoundError(f"Cannot locate business data for {business_id!r} under {data_root}")


def _split_resource(data_root: Path, filename: str) -> Path | None:
    for parent in (data_root, *data_root.parents):
        candidate = parent / filename
        if candidate.is_file():
            return candidate
    return None


def run(
    questions_path: Path,
    data_root: Path,
    output_path: Path,
    log_dir: Path | None = None,
    rulebook_path: Path | None = None,
    calendar_path: Path | None = None,
) -> int:
    agents: dict[str, DataMateAgent] = {}
    rulebook_path = rulebook_path or _split_resource(data_root, "rulebook.md")
    calendar_path = calendar_path or _split_resource(data_root, "calendar.csv")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
    with questions_path.open("r", encoding="utf-8") as questions, output_path.open(
        "w", encoding="utf-8"
    ) as output:
        for line_number, raw in enumerate(questions, start=1):
            if not raw.strip():
                continue
            try:
                question: dict[str, Any] = json.loads(raw)
                identifier = question["id"]
                business_id = question["business_id"]
                text = question["question"]
                as_of_date = question["as_of_date"]
            except (json.JSONDecodeError, KeyError, TypeError) as error:
                raise ValueError(f"Invalid question at {questions_path}:{line_number}: {error}") from error
            if not all(isinstance(value, str) for value in (identifier, business_id, text, as_of_date)):
                raise ValueError(f"Invalid field types at {questions_path}:{line_number}")
            started = time.perf_counter()
            agent = agents.get(business_id)
            if agent is None:
                agent = DataMateAgent(
                    _business_path(data_root, business_id),
                    rulebook_path=rulebook_path,
                    calendar_path=calendar_path,
                )
                agents[business_id] = agent
            answer = agent.answer(text, as_of_date)
            result = {"id": identifier, **answer}
            output.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
            output.flush()
            if log_dir is not None:
                safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", identifier)
                log = {
                    "id": identifier,
                    "business_id": business_id,
                    "question": text,
                    "final_answer": result,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                }
                (log_dir / f"{safe_id}.json").write_text(
                    json.dumps(log, ensure_ascii=False, allow_nan=False) + "\n",
                    encoding="utf-8",
                )
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Answer Vietnamese questions over CSV, Excel, and SQLite business data."
    )
    parser.add_argument("--questions", type=Path, required=True, help="Input questions.jsonl")
    parser.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="Directory containing business_id folders or a single business package",
    )
    parser.add_argument("--output", type=Path, required=True, help="Output submission.jsonl")
    parser.add_argument("--logs", type=Path, help="Optional directory for per-question JSON logs")
    parser.add_argument("--rulebook", type=Path, help="Split rulebook (auto-detected if omitted)")
    parser.add_argument("--calendar", type=Path, help="Split lunar calendar (auto-detected if omitted)")
    args = parser.parse_args(argv)
    try:
        run(args.questions, args.data_dir, args.output, args.logs, args.rulebook, args.calendar)
    except (OSError, ValueError) as error:
        print(f"datamate: {error}", file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
