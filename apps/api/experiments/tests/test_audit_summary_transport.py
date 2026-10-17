from __future__ import annotations

import calendar
import json
from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from experiments import audit_summary


def _response(count="9007199254740993"):
    return {
        "month": "2024-02",
        "timezone": "UTC",
        "materialized_through": None,
        "totals": {"event_count": count, "error_count": "0", "action_kind_count": 1},
        "daily": [
            {
                "day": date(2024, 2, day).isoformat(),
                "event_count": count if day == 1 else "0",
                "error_count": "0",
            }
            for day in range(1, calendar.monthrange(2024, 2)[1] + 1)
        ],
        "top_actions": [{"key": "project.create", "event_count": count}],
        "target_types": [{"key": "", "event_count": count}],
        "actor_roles": [{"key": "super_admin", "event_count": count}],
    }


def _bind_transport(monkeypatch, handler):
    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        audit_summary.httpx,
        "AsyncClient",
        lambda **options: client_type(transport=transport, **options),
    )


async def test_summary_transport_preserves_large_counts_and_sends_only_grouped_facts(
    monkeypatch,
):
    requests = []

    def handle(request):
        requests.append(request)
        assert json.loads(request.content) == {
            "month": "2024-02",
            "materialized_through": None,
            "rows": [
                {
                    "day": "2024-02-01",
                    "action": "project.create",
                    "target_type": "",
                    "actor_role": "super_admin",
                    "status_family": 2,
                    "event_count": "9007199254740993",
                }
            ],
        }
        return httpx.Response(200, json=_response())

    _bind_transport(monkeypatch, handle)
    result = await audit_summary.monthly_audit_summary(
        month="2024-02",
        materialized_through=None,
        rows=[
            SimpleNamespace(
                day=date(2024, 2, 1),
                action="project.create",
                target_type="",
                actor_role="super_admin",
                status_family=2,
                event_count=9007199254740993,
            )
        ],
    )
    assert result.totals.event_count == 9007199254740993
    assert result.daily[0].event_count == 9007199254740993
    assert len(result.daily) == 29
    assert result.target_types[0].key == ""
    assert len(requests) == 1


@pytest.mark.parametrize(
    "bad_count", [9007199254740993, "01", "-1", "1.0", "NaN", None]
)
async def test_summary_rejects_inexact_or_invalid_wire_counts(monkeypatch, bad_count):
    _bind_transport(
        monkeypatch, lambda _: httpx.Response(200, json=_response(bad_count))
    )
    with pytest.raises(audit_summary.AuditSummaryUnavailable, match="invalid_response"):
        await audit_summary.monthly_audit_summary(
            month="2024-02", materialized_through=None, rows=[]
        )


@pytest.mark.parametrize(
    "failure",
    [
        "timeout",
        "redirect",
        "unavailable",
        "invalid_json",
        "wrong_month",
        "wrong_materialization",
    ],
)
async def test_summary_failure_never_returns_empty_or_local_results(
    monkeypatch, failure
):
    calls = []

    def handle(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("fixture timeout", request=request)
        if failure == "redirect":
            return httpx.Response(
                302, headers={"Location": "http://other.test/private"}
            )
        if failure == "unavailable":
            return httpx.Response(
                503, json={"code": "busy", "message": "busy", "retryable": True}
            )
        if failure == "invalid_json":
            return httpx.Response(200, content=b"not-json")
        result = _response()
        if failure == "wrong_month":
            result["month"] = "2024-03"
        else:
            result["materialized_through"] = "2024-02-28"
        return httpx.Response(200, json=result)

    _bind_transport(monkeypatch, handle)
    with pytest.raises(audit_summary.AuditSummaryUnavailable):
        await audit_summary.monthly_audit_summary(
            month="2024-02", materialized_through=None, rows=[]
        )
    assert len(calls) == 1


async def test_summary_bounds_both_request_and_response(monkeypatch):
    calls = []
    monkeypatch.setattr(audit_summary, "_MAX_BYTES", 512)

    def handle(request):
        calls.append(request)
        return httpx.Response(200, content=b"x" * 513)

    _bind_transport(monkeypatch, handle)
    with pytest.raises(audit_summary.AuditSummaryUnavailable, match="invalid_response"):
        await audit_summary.monthly_audit_summary(
            month="2024-02", materialized_through=None, rows=[]
        )
    assert len(calls) == 1

    row = SimpleNamespace(
        day=date(2024, 2, 1),
        action="x" * 500,
        target_type="",
        actor_role="",
        status_family=0,
        event_count=1,
    )
    with pytest.raises(
        audit_summary.AuditSummaryUnavailable, match="capacity_exceeded"
    ):
        await audit_summary.monthly_audit_summary(
            month="2024-02", materialized_through=None, rows=[row]
        )
    assert len(calls) == 1


async def test_request_budget_stops_encoding_before_reading_all_rows(monkeypatch):
    monkeypatch.setattr(audit_summary, "_MAX_BYTES", 512)
    consumed = []

    def rows():
        for index in range(100000):
            consumed.append(index)
            yield SimpleNamespace(
                day=date(2024, 2, 1),
                action="project.create",
                target_type="project",
                actor_role="super_admin",
                status_family=2,
                event_count=1,
            )

    with pytest.raises(
        audit_summary.AuditSummaryUnavailable, match="capacity_exceeded"
    ):
        async for _ in audit_summary._request_body("2024-02", None, rows()):
            pass
    assert 0 < len(consumed) < 10
