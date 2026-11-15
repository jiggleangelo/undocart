import argparse
import csv
import json
from datetime import date

from .classify import classify
from .models import Case
from .sample_data import make_sources

MAX_LINE = 65536


def _cell(value):
    text = ("" if value is None else str(value))[:500]
    dangerous = text.startswith(("\t", "\r", "\n")) or text.lstrip().startswith(("=", "+", "-", "@"))
    return "'" + text if dangerous else text


def _lines(source):
    while True:
        line = source.readline(MAX_LINE + 1)
        if not line:
            return
        if len(line) > MAX_LINE:
            while line and not line.endswith("\n"):
                line = source.readline(MAX_LINE + 1)
            yield None
        else:
            yield line


def run(input_path, output_path, today):
    if today is None:
        raise ValueError("today is required for deterministic batch classification")
    sources = make_sources()
    fields = ["case_id", "route", "sub", "reasons"]
    with open(input_path, encoding="utf-8") as source, open(output_path, "w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        for line_number, line in enumerate(_lines(source), 1):
            if line is not None and not line.strip():
                continue
            try:
                if line is None:
                    raise ValueError("line too large")
                case = Case.from_dict(json.loads(line))
                decision = classify(case, sources, today)
                row = {"case_id": case.case_id, "route": decision.route, "sub": decision.sub,
                       "reasons": "|".join(decision.reasons)}
            except Exception:
                # No rejected claimant data is copied into a review CSV.
                row = {"case_id": f"line-{line_number}", "route": "ERROR", "sub": "ERROR_INVALID_CASE",
                       "reasons": "INVALID_INPUT"}
            writer.writerow({key: _cell(row[key]) for key in fields})


def main():
    parser = argparse.ArgumentParser(description="Classify anonymised Remedy JSONL cases")
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--today", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    run(args.input, args.output, args.today)


if __name__ == "__main__":
    main()
