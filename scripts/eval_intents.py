#!/usr/bin/env python3
"""Evaluation harness for the intent parser (M3).

Runs every fixture in tests/fixtures/intents/manifest.json through a real
POST /intents call (no mocking — this hits the actual vision provider) and
reports:
  - template match rate: predicted template_id == ground_truth.template_id
  - dimension MAE (mm): mean absolute error vs ground_truth.dimensions_mm
  - critical-dim question coverage: did the parser ask about every
    dimension listed in a fixture's ground_truth.dimensions_mm?

Usage:
    python scripts/eval_intents.py --provider openai
    python scripts/eval_intents.py --provider anthropic
    python scripts/eval_intents.py --provider openai --manifest path/to/manifest.json

This boots its own `uvicorn api.main:app` subprocess with VISION_PROVIDER set
to the requested provider — the "one env var + a restart" switch described in
README.md/CLAUDE.md, just automated here so you can compare both providers on
one command each without hand-editing .env in between. Your real .env's other
settings (the provider's API key, etc.) are inherited as-is.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from statistics import mean
from typing import Any

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = BASE_DIR / "tests" / "fixtures" / "intents" / "manifest.json"


def load_manifest(path: Path) -> list[dict[str, Any]]:
    with open(path) as f:
        return json.load(f)["fixtures"]


def start_server(provider: str, port: int) -> subprocess.Popen:
    env = {**os.environ, "VISION_PROVIDER": provider}
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.main:app", "--port", str(port)],
        cwd=BASE_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def wait_for_server(
    base_url: str, proc: subprocess.Popen, timeout: float = 20.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(
                f"Server process exited early (code {proc.returncode}):\n{output}"
            )
        try:
            if httpx.get(f"{base_url}/health", timeout=1.0).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.3)
    raise TimeoutError(f"Server did not become healthy within {timeout}s.")


def run_fixture(
    base_url: str, manifest_dir: Path, fixture: dict[str, Any]
) -> dict[str, Any]:
    opened = [open(manifest_dir / p, "rb") for p in fixture["photos"]]
    try:
        files = [
            ("photos", (Path(p).name, f, "image/jpeg"))
            for p, f in zip(fixture["photos"], opened)
        ]
        data = {"text": fixture["text"]}
        if fixture.get("annotation"):
            data["annotation"] = json.dumps(fixture["annotation"])
        response = httpx.post(
            f"{base_url}/intents", files=files, data=data, timeout=90.0
        )
    finally:
        for f in opened:
            f.close()

    if response.status_code != 200:
        return {
            "id": fixture["id"],
            "error": f"HTTP {response.status_code}: {response.text[:300]}",
        }
    return {"id": fixture["id"], "intent": response.json()}


def score(
    fixtures: list[dict[str, Any]], results: list[dict[str, Any]]
) -> dict[str, Any]:
    by_id = {r["id"]: r for r in results}
    template_matches = 0
    template_total = 0
    all_dim_errors: list[float] = []
    critical_hits = 0
    critical_total = 0
    rows = []

    for fixture in fixtures:
        result = by_id.get(fixture["id"])
        gt = fixture.get("ground_truth", {})
        row: dict[str, Any] = {"id": fixture["id"]}

        if result is None or "error" in result:
            row["error"] = result["error"] if result else "no result"
            rows.append(row)
            continue

        intent = result["intent"]
        predicted_template = intent.get("template_id")
        expected_template = gt.get("template_id")

        template_total += 1
        template_match = predicted_template == expected_template
        template_matches += int(template_match)
        row["template_match"] = template_match
        row["predicted_template_id"] = predicted_template
        row["expected_template_id"] = expected_template

        dims_by_name = {d["name"]: d for d in intent.get("dimensions", [])}
        gt_dims = gt.get("dimensions_mm", {})
        fixture_errors = []
        for name, true_value in gt_dims.items():
            dim = dims_by_name.get(name)
            if dim is None or dim.get("value_mm") is None:
                continue
            error = abs(dim["value_mm"] - true_value)
            fixture_errors.append(error)
            all_dim_errors.append(error)
        row["dimension_mae"] = mean(fixture_errors) if fixture_errors else None

        asked_dims = {q.get("dim_name") for q in intent.get("questions", [])}
        for name in gt_dims:
            critical_total += 1
            critical_hits += int(name in asked_dims)
        row["critical_dims_missing"] = sorted(set(gt_dims) - asked_dims)

        rows.append(row)

    return {
        "template_match_rate": (
            template_matches / template_total if template_total else None
        ),
        "template_matches": template_matches,
        "template_total": template_total,
        "dimension_mae_overall": mean(all_dim_errors) if all_dim_errors else None,
        "critical_dim_coverage_rate": (
            critical_hits / critical_total if critical_total else None
        ),
        "fixtures": rows,
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def print_report(provider: str, report: dict[str, Any]) -> None:
    print()
    print(f"=== eval_intents report (provider={provider}) ===")
    print(
        f"template match rate: {_pct(report['template_match_rate'])} "
        f"({report['template_matches']}/{report['template_total']})"
    )
    print(f"dimension MAE (mm), overall: {_fmt(report['dimension_mae_overall'])}")
    print(
        f"critical-dim question coverage: {_pct(report['critical_dim_coverage_rate'])}"
    )
    print()
    for row in report["fixtures"]:
        if "error" in row:
            print(f"  [{row['id']}] ERROR: {row['error']}")
            continue
        if row["template_match"]:
            match_str = "MATCH"
        else:
            match_str = f"MISS (got {row['predicted_template_id']!r}, want {row['expected_template_id']!r})"
        missing = row["critical_dims_missing"]
        missing_str = f", missing questions for: {missing}" if missing else ""
        print(
            f"  [{row['id']}] template: {match_str}, dim MAE: {_fmt(row['dimension_mae'])}mm{missing_str}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--provider", choices=["openai", "anthropic"], required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--port", type=int, default=8931)
    args = parser.parse_args()

    manifest_dir = args.manifest.resolve().parent
    fixtures = load_manifest(args.manifest)
    base_url = f"http://127.0.0.1:{args.port}"

    print(
        f"Starting server with VISION_PROVIDER={args.provider} on port {args.port}..."
    )
    proc = start_server(args.provider, args.port)
    try:
        wait_for_server(base_url, proc)
        print(f"Server ready. Running {len(fixtures)} fixture(s)...")

        results = []
        for fixture in fixtures:
            print(f"  {fixture['id']}...", end=" ", flush=True)
            result = run_fixture(base_url, manifest_dir, fixture)
            print("error" if "error" in result else "ok")
            results.append(result)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print_report(args.provider, score(fixtures, results))


if __name__ == "__main__":
    main()
