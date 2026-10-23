"""
BrollEngine API Test Suite
Covers: Integration Standard, must-haves, hard cases, idempotency, error handling.
Run with: pytest tests/test_api.py -v
Requires the API to be running at http://localhost:8000 (or set BROLL_API_URL).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest
import pytest_asyncio

BASE_URL = os.getenv("BROLL_API_URL", "http://localhost:8000")
POLL_INTERVAL = int(os.getenv("TEST_POLL_INTERVAL", "3"))
JOB_TIMEOUT = int(os.getenv("TEST_JOB_TIMEOUT", "300"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def wait_for_job(client: httpx.AsyncClient, job_id: str, timeout: int = JOB_TIMEOUT) -> Dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = await client.get(f"{BASE_URL}/api/v1/broll/jobs/{job_id}")
        data = resp.json()
        if data.get("status") in ("succeeded", "failed"):
            return data
        await asyncio.sleep(POLL_INTERVAL)
    return {"status": "timeout", "job_id": job_id}


async def submit_and_wait(
    client: httpx.AsyncClient,
    body: Dict,
    idempotency_key: str | None = None,
) -> tuple[Dict, Dict]:
    headers = {}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    resp = await client.post(
        f"{BASE_URL}/api/v1/broll/jobs",
        json=body,
        headers=headers,
        timeout=30,
    )
    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
    submit_data = resp.json()
    assert "job_id" in submit_data

    final = await wait_for_job(client, submit_data["job_id"])
    return submit_data, final


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_healthz(client):
    """Integration Standard #8: /healthz must return 200."""
    resp = await client.get(f"{BASE_URL}/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") == "ok"


@pytest.mark.asyncio
async def test_ready(client):
    """Integration Standard #8: /ready must check dependencies."""
    resp = await client.get(f"{BASE_URL}/ready")
    assert resp.status_code in (200, 503)  # 503 ok if deps not started
    data = resp.json()
    assert "checks" in data


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_input_returns_422(client):
    """Must-have #1 / Integration Standard #5: empty body → structured error."""
    resp = await client.post(
        f"{BASE_URL}/api/v1/broll/jobs",
        json={},
        timeout=10,
    )
    assert resp.status_code in (422, 400)
    data = resp.json()
    assert "error" in data
    assert "code" in data["error"]
    assert "message" in data["error"]
    assert "retryable" in data["error"]


@pytest.mark.asyncio
async def test_invalid_aspect_ratio(client):
    """Integration Standard #5: clear error for unsupported aspect ratio."""
    resp = await client.post(
        f"{BASE_URL}/api/v1/broll/jobs",
        json={"script": "Hello world", "aspect_ratios": ["4:3"]},
        timeout=10,
    )
    # Should either be 422 or succeed with a warning — not crash
    assert resp.status_code in (202, 422, 400)


# ---------------------------------------------------------------------------
# Idempotency (Integration Standard #4)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_idempotency_same_key_returns_same_job(client):
    """Sending the same Idempotency-Key twice must return the same job_id."""
    key = f"test-idem-{uuid.uuid4()}"
    body = {"script": "Test idempotency", "aspect_ratios": ["16:9"]}

    resp1 = await client.post(
        f"{BASE_URL}/api/v1/broll/jobs",
        json=body,
        headers={"Idempotency-Key": key},
        timeout=15,
    )
    resp2 = await client.post(
        f"{BASE_URL}/api/v1/broll/jobs",
        json=body,
        headers={"Idempotency-Key": key},
        timeout=15,
    )

    assert resp1.status_code == 202
    assert resp2.status_code == 202
    assert resp1.json()["job_id"] == resp2.json()["job_id"], "Idempotency broken — different job_ids!"


# ---------------------------------------------------------------------------
# Job lifecycle (Integration Standard #3)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_job_returns_202_with_job_id(client):
    """POST must return 202 with a job_id immediately (background job)."""
    resp = await client.post(
        f"{BASE_URL}/api/v1/broll/jobs",
        json={"script": "Quick test script for a promo video.", "aspect_ratios": ["16:9"]},
        timeout=15,
    )
    assert resp.status_code == 202
    data = resp.json()
    assert "job_id" in data
    assert "poll_url" in data
    assert data.get("status") == "queued"


@pytest.mark.asyncio
async def test_job_status_not_found(client):
    """GET with non-existent job_id → structured 404."""
    resp = await client.get(f"{BASE_URL}/api/v1/broll/jobs/nonexistent-job-id")
    assert resp.status_code == 404
    data = resp.json()
    assert "error" in data
    assert data["error"]["code"] == "JOB_NOT_FOUND"


# ---------------------------------------------------------------------------
# Full pipeline tests (must-haves)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_script_input_produces_video(client):
    """Must-have #1: text script input → video output."""
    body = {
        "script": "Learning Python is easier than you think. Start small and practice daily.",
        "voice": "en-US-JennyNeural",
        "aspect_ratios": ["16:9"],
        "captions": {"srt": True, "burn_in": False},
    }
    submit, final = await submit_and_wait(client, body)

    assert final["status"] == "succeeded", f"Job failed: {final.get('error')}"
    outputs = final.get("outputs", {})
    assert outputs.get("video_16x9"), "video_16x9 URL missing"
    assert outputs.get("captions_srt"), "captions.srt URL missing"
    assert outputs.get("timeline_json"), "timeline.json URL missing"
    assert outputs.get("manifest_json"), "manifest.json URL missing"


@pytest.mark.asyncio
async def test_timeline_json_structure(client):
    """Must-have #3: timeline.json contains source, licence, attribution per clip."""
    body = {
        "script": "The ocean covers more than seventy percent of the Earth surface.",
        "aspect_ratios": ["16:9"],
    }
    submit, final = await submit_and_wait(client, body)
    assert final["status"] == "succeeded"

    timeline_url = final["outputs"].get("timeline_json")
    if timeline_url:
        resp = await client.get(timeline_url, timeout=15)
        entries = resp.json()
        assert isinstance(entries, list), "timeline.json should be a list"
        assert len(entries) > 0

        first = entries[0]
        clip = first.get("clip", {})
        assert clip.get("clip_source"), "clip_source missing"
        assert clip.get("licence"), "licence missing"
        assert clip.get("attribution"), "attribution missing"
        assert "relevance_score" in clip, "relevance_score missing"
        assert 1.0 <= clip["relevance_score"] <= 5.0, "score out of range"


@pytest.mark.asyncio
async def test_dual_aspect_ratios(client):
    """Must-have #4: both 16:9 and 9:16 videos produced."""
    body = {
        "script": "Nature is beautiful. Every forest tells a story of growth and survival.",
        "aspect_ratios": ["16:9", "9:16"],
    }
    submit, final = await submit_and_wait(client, body)
    assert final["status"] == "succeeded"

    outputs = final.get("outputs", {})
    assert outputs.get("video_16x9"), "16:9 video missing"
    assert outputs.get("video_9x16"), "9:16 video missing"


@pytest.mark.asyncio
async def test_hard_case_statistics_and_abstract(client):
    """Hard case: statistics ('40%') and abstract phrases ('trust is earned slowly')."""
    body = {
        "script": "Sales grew 40 percent in 2025. Trust is earned slowly. Our product reached one million users.",
        "aspect_ratios": ["16:9"],
    }
    resp = await client.post(f"{BASE_URL}/api/v1/broll/jobs", json=body, timeout=15)
    assert resp.status_code == 202  # Must not crash; handles gracefully


@pytest.mark.asyncio
async def test_hard_case_empty_script(client):
    """Hard case: empty script → clear error, no crash."""
    resp = await client.post(
        f"{BASE_URL}/api/v1/broll/jobs",
        json={"script": "", "aspect_ratios": ["16:9"]},
        timeout=10,
    )
    # Should return a structured error
    assert resp.status_code in (400, 422)
    data = resp.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_manifest_json_structure(client):
    """Integration Standard #9: manifest.json contains models, providers, timings, cost."""
    body = {
        "script": "Artificial intelligence is reshaping every industry.",
        "aspect_ratios": ["16:9"],
    }
    submit, final = await submit_and_wait(client, body)
    assert final["status"] == "succeeded"

    manifest_url = final["outputs"].get("manifest_json")
    if manifest_url:
        resp = await client.get(manifest_url, timeout=15)
        m = resp.json()
        assert "models" in m
        assert "providers" in m
        assert "timings" in m
        assert "cost_estimate_usd" in m
        assert "licence_summary" in m


# ---------------------------------------------------------------------------
# Concurrent submissions (hard case)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_submissions(client):
    """Hard case: two users submitting simultaneously — both should succeed."""
    body1 = {"script": "First concurrent submission test.", "aspect_ratios": ["16:9"]}
    body2 = {"script": "Second concurrent submission test.", "aspect_ratios": ["16:9"]}

    resp1_task = asyncio.create_task(
        client.post(f"{BASE_URL}/api/v1/broll/jobs", json=body1, timeout=15)
    )
    resp2_task = asyncio.create_task(
        client.post(f"{BASE_URL}/api/v1/broll/jobs", json=body2, timeout=15)
    )

    resp1, resp2 = await asyncio.gather(resp1_task, resp2_task)
    assert resp1.status_code == 202
    assert resp2.status_code == 202
    assert resp1.json()["job_id"] != resp2.json()["job_id"], "Should have different job IDs"
