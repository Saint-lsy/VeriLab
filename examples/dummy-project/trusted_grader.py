from __future__ import annotations

import argparse
import json
from pathlib import Path

TRUTH = {"a": 0, "b": 1, "c": 1, "d": 0}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    prediction_rows = [item for item in manifest["artifacts"] if item["role"] == "predictions"]
    if len(prediction_rows) != 1:
        raise SystemExit("exactly one predictions artifact is required")
    predictions = json.loads(Path(prediction_rows[0]["absolute_path"]).read_text(encoding="utf-8"))
    pairs = list(zip(predictions["case_ids"], predictions["predictions"], strict=True))
    if [case_id for case_id, _ in pairs] != list(TRUTH):
        raise SystemExit("prediction cohort does not match the locked dummy cohort")
    accuracy = sum(int(TRUTH[case_id] == value) for case_id, value in pairs) / len(TRUTH)
    output = {
        "schema_version": 1,
        "protocol_id": "public-oof-v1",
        "cohort": "dummy-four-cases",
        "metrics": {"accuracy": accuracy},
        "details": {"case_count": len(TRUTH)},
    }
    Path(args.output).write_text(
        json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
