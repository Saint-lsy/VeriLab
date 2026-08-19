from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions", default="0,1,0,0")
    parser.add_argument("--reported-score", type=float, default=0.75)
    args = parser.parse_args()
    run_dir = Path(os.environ["VERILAB_RUN_DIR"])
    outputs = run_dir / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    predictions = [int(value) for value in args.predictions.split(",")]
    (outputs / "predictions.json").write_text(
        json.dumps({"case_ids": ["a", "b", "c", "d"], "predictions": predictions}) + "\n",
        encoding="utf-8",
    )
    (outputs / "reported_metrics.json").write_text(
        json.dumps({"metrics": {"accuracy": args.reported_score}}) + "\n",
        encoding="utf-8",
    )
    (outputs / "checkpoint.bin").write_bytes(b"dummy-checkpoint-v1\n")


if __name__ == "__main__":
    main()
