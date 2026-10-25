#!/usr/bin/env python3
"""
Batch runner for BrollEngine — Integration Standard requirement #10.

Usage:
    python batch_runner.py --test-case tests/TEST_CASE [--base-url http://localhost:8000]

The test case directory must contain one or more JSON files with job request bodies.
Results are written to the batch output directory (default: batch_results/).

Each result file contains:
  - The input request
  - The API response (job_id, status poll)
  - The final job outputs (video URLs, timeline, manifest)
  - Elapsed time and any errors
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BrollEngine Batch Runner")
    parser.add_argument(
        "--test-case",
        required=True,
        help="Path to directory or single JSON file containing test job requests.",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("BROLL_API_URL", "http://localhost:8000"),
        help="Base URL of the running BrollEngine API (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--output-dir",
        default=os.getenv("BATCH_OUTPUT_DIR", "batch_results"),
        help="Directory to write result files to.",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=5,
        help="Seconds between job status polls (default: 5)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="Max seconds to wait per job (default: 900)",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Job submission and polling
# ---------------------------------------------------------------------------

async def submit_job(client: httpx.AsyncClient, base_url: str, request_body: Dict) -> Dict:
    """POST a job and return the response JSON."""
    resp = await client.post(
        f"{base_url}/api/v1/broll/jobs",
        json=request_body,
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


async def poll_until_done(
    client: httpx.AsyncClient,
    base_url: str,
    job_id: str,
    poll_interval: int,
    timeout: int,
) -> Dict:
    """Poll GET /api/v1/broll/jobs/{job_id} until status is succeeded or failed."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = await client.get(f"{base_url}/api/v1/broll/jobs/{job_id}", timeout=25.0)
            if resp.status_code == 200:
                data = resp.json()
                status = data.get("status", "queued")
                progress = data.get("progress", 0)
                print(f"  [{job_id[:8]}] status={status} progress={progress}%")
                if status in ("succeeded", "failed"):
                    return data
        except Exception as poll_err:
            print(f"  [{job_id[:8]}] transient poll note: {poll_err}, retrying in {poll_interval}s...")
        await asyncio.sleep(poll_interval)

    return {"job_id": job_id, "status": "timeout", "error": {"code": "TIMEOUT", "message": f"Job did not complete within {timeout}s"}}


# ---------------------------------------------------------------------------
# Load test cases
# ---------------------------------------------------------------------------

def load_test_cases(test_case_path: str) -> List[Dict[str, Any]]:
    p = Path(test_case_path)
    cases = []
    if p.is_dir():
        for f in sorted(p.glob("*.json")):
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
                data["_source_file"] = f.name
                cases.append(data)
    elif p.is_file():
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, list):
                cases = data
            else:
                data["_source_file"] = p.name
                cases = [data]
    else:
        print(f"ERROR: test case path not found: {test_case_path}", file=sys.stderr)
        sys.exit(1)

    if not cases:
        print(f"ERROR: No JSON test cases found in {test_case_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(cases)} test case(s) from {test_case_path}")
    return cases


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_batch(args: argparse.Namespace) -> None:
    cases = load_test_cases(args.test_case)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    summary_rows = []

    async with httpx.AsyncClient() as client:
        for i, case in enumerate(cases):
            source_file = case.pop("_source_file", f"case_{i+1:03d}.json")
            job_name = source_file.replace(".json", "")

            print(f"\n[{i+1}/{len(cases)}] Submitting: {job_name}")
            t_start = time.perf_counter()

            result: Dict[str, Any] = {
                "test_case": job_name,
                "request": case,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            }

            try:
                submit_resp = await submit_job(client, args.base_url, case)
                job_id = submit_resp.get("job_id")
                result["job_id"] = job_id
                result["submit_response"] = submit_resp

                if not job_id:
                    result["status"] = "submit_failed"
                    result["error"] = submit_resp
                else:
                    print(f"  Job accepted: {job_id}")
                    final = await poll_until_done(
                        client, args.base_url, job_id, args.poll_interval, args.timeout
                    )
                    result["final_status"] = final
                    result["status"] = final.get("status", "unknown")

            except Exception as exc:
                result["status"] = "error"
                result["error"] = str(exc)
                print(f"  ERROR: {exc}")

            elapsed = time.perf_counter() - t_start
            result["elapsed_sec"] = round(elapsed, 2)

            # Write individual result file
            result_file = output_dir / f"{job_name}_result.json"
            with open(result_file, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            print(f"  Result saved: {result_file} ({elapsed:.1f}s)")

            summary_rows.append({
                "test_case": job_name,
                "status": result["status"],
                "elapsed_sec": result["elapsed_sec"],
            })

    # Write summary
    summary_file = output_dir / "batch_summary.json"
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump({
            "run_at": datetime.now(timezone.utc).isoformat(),
            "base_url": args.base_url,
            "total_cases": len(cases),
            "passed": sum(1 for r in summary_rows if r["status"] == "succeeded"),
            "failed": sum(1 for r in summary_rows if r["status"] != "succeeded"),
            "results": summary_rows,
        }, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"Batch complete. Summary: {summary_file}")
    passed = sum(1 for r in summary_rows if r["status"] == "succeeded")
    print(f"  Passed: {passed}/{len(cases)}")


def main():
    args = parse_args()
    asyncio.run(run_batch(args))


if __name__ == "__main__":
    main()
