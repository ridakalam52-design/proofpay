"""Asynchronous AI: instant rule-based draft + background refinement, background proof review,
fail-safe money rules, no double capture, restart recovery, and the Gemini fallback model."""
import json
import threading
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ai import AIError, GeminiAgent, MockAgent
from app.main import create_app
from app.paypal import MockPayPal

from .test_flow import DESC, create, fund, submit, token_of


class FakeGemini:
    """Behaves like a real (slow) model: mode isn't 'mock', so the app takes the async path."""

    mode = "gemini:fake"

    def __init__(self, gate: threading.Event | None = None, parse_error=False, verify_error=False, verdict=None):
        self.gate = gate
        self.parse_error = parse_error
        self.verify_error = verify_error
        self.verdict = verdict
        self.parse_calls = 0
        self.verify_calls = 0

    def _wait(self):
        if self.gate is not None:
            assert self.gate.wait(10), "test gate never opened"

    def parse_deal(self, description, today=None):
        self.parse_calls += 1
        self._wait()
        if self.parse_error:
            raise AIError("Gemini request failed: ReadTimeout")
        d = MockAgent().parse_deal(description)
        d["task"] = "Paint the balcony wall white (refined by AI)"
        d["checklist"] = ["Whole balcony wall is white", "No drips or streaks", "Floor and glass are clean"]
        return d

    def verify_proof(self, deal, note, photos, signals=None, filenames=None):
        self.verify_calls += 1
        self._wait()
        if self.verify_error:
            raise AIError("Gemini API 503: overloaded")
        if self.verdict is not None:
            return self.verdict(deal)
        return MockAgent().verify_proof(deal, note, photos, signals=signals, filenames=filenames)


def wait_for(fn, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def make_client(settings, agent, gateway=None, background="thread"):
    s = replace(settings, ai_background=background)
    app = create_app(s, agent=agent, gateway=gateway or MockPayPal())
    return TestClient(app), app


# ------------------------------------------------------------ deal refinement
def test_create_returns_rule_based_draft_then_refines_in_background(settings):
    gate = threading.Event()
    agent = FakeGemini(gate=gate)
    client, _ = make_client(settings, agent)
    t0 = time.monotonic()
    d = create(client)
    assert time.monotonic() - t0 < 2  # did not wait for the model
    assert d["ai_status"] == "refining" and d["status"] == "draft"
    assert d["worker_name"] == "Ravi" and d["amount"] == "40.00"  # rule-based parse, usable immediately
    assert "refined" not in d["task"]
    ct = token_of(d["client_url"])
    gate.set()
    done = wait_for(lambda: (r := client.get(f"/api/c/{ct}").json())["ai_status"] == "done" and r)
    assert "refined by AI" in done["task"]
    assert done["checklist"][0] == "Whole balcony wall is white"
    assert done["ai_mode"] == "gemini:fake"
    assert "refined" in [e["kind"] for e in done["events"]]


def test_refinement_does_not_overwrite_client_edits(settings):
    gate = threading.Event()
    client, _ = make_client(settings, FakeGemini(gate=gate))
    d = create(client)
    ct = token_of(d["client_url"])
    assert client.patch(f"/api/c/{ct}", json={"task": "My own wording", "amount": "55"}).status_code == 200
    gate.set()
    done = wait_for(lambda: (r := client.get(f"/api/c/{ct}").json())["ai_status"] == "done" and r)
    assert done["task"] == "My own wording" and done["amount"] == "55.00"
    assert "refine_skipped" in [e["kind"] for e in done["events"]]


def test_refinement_after_funding_is_only_logged(settings):
    gate = threading.Event()
    client, _ = make_client(settings, FakeGemini(gate=gate))
    d = create(client)
    ct, funded = fund(client, d)
    gate.set()
    done = wait_for(lambda: (r := client.get(f"/api/c/{ct}").json())["ai_status"] == "done" and r)
    assert done["status"] == "funded" and done["task"] == funded["task"]
    assert "refine_skipped" in [e["kind"] for e in done["events"]]


def test_refinement_failure_keeps_rule_based_fields(settings):
    client, _ = make_client(settings, FakeGemini(parse_error=True), background="inline")
    d = create(client)
    assert d["ai_status"] == "failed"
    assert d["worker_name"] == "Ravi" and d["amount"] == "40.00"
    assert any(e["kind"] == "warning" and "rule-based" in e["message"] for e in d["events"])


def test_inline_setting_runs_refinement_synchronously(settings):
    client, _ = make_client(settings, FakeGemini(), background="inline")
    d = create(client)
    assert d["ai_status"] == "done" and "refined by AI" in d["task"]


# ---------------------------------------------------------- async proof review
def test_proof_upload_returns_immediately_then_ai_captures(settings, png):
    gate = threading.Event()
    gw = MockPayPal()
    client, _ = make_client(settings, FakeGemini(gate=gate), gateway=gw)
    gate.set()  # let refinement finish
    d = create(client)
    ct = token_of(d["client_url"])
    wait_for(lambda: client.get(f"/api/c/{ct}").json()["ai_status"] == "done")
    d = client.get(f"/api/c/{ct}").json()
    ct, funded = fund(client, d)
    gate.clear()

    t0 = time.monotonic()
    r = submit(client, d, "Two coats of white done, edges taped, floor cleaned.", [("wall.png", png(11))])
    assert r.status_code == 200 and time.monotonic() - t0 < 2
    w = r.json()
    assert w["status"] == "verifying"
    assert w["submissions"][-1]["status"] == "ai_reviewing" and w["submissions"][-1]["verdict"] is None
    # money can't move while the AI is reviewing
    assert client.post(f"/api/c/{ct}/release").status_code == 409
    assert client.post(f"/api/c/{ct}/void").status_code == 409
    assert submit(client, d, "again", [("w2.png", png(12))]).status_code == 409

    gate.set()
    c = wait_for(lambda: (r := client.get(f"/api/c/{ct}").json())["status"] == "paid" and r)
    assert c["submissions"][-1]["status"] == "done" and c["submissions"][-1]["decision"] == "auto_release"
    assert gw.auths[funded["authorization_id"]]["status"] == "CAPTURED"
    assert [e["kind"] for e in c["events"]].count("captured") == 1


def test_ai_error_goes_to_client_review_never_pays(settings, png):
    gw = MockPayPal()
    client, _ = make_client(settings, FakeGemini(verify_error=True), gateway=gw, background="inline")
    d = create(client)
    ct, funded = fund(client, d)
    w = submit(client, d, "All done.", [("wall.png", png(13))]).json()
    assert w["status"] == "needs_review"
    v = w["submissions"][-1]["verdict"]
    assert v["agent"] == "error" and "503" in v["summary"]
    assert w["submissions"][-1]["decision"] == "needs_review"
    assert gw.auths[funded["authorization_id"]]["status"] == "CREATED"  # still only held


def test_verdict_applied_once_no_double_capture(settings, png):
    gw = MockPayPal()
    captures = []
    orig = gw.capture_authorization

    def counting(*a, **k):
        captures.append(a)
        return orig(*a, **k)

    gw.capture_authorization = counting
    gate = threading.Event()
    client, app = make_client(settings, FakeGemini(gate=gate), gateway=gw)
    gate.set()
    d = create(client)
    ct = token_of(d["client_url"])
    wait_for(lambda: client.get(f"/api/c/{ct}").json()["ai_status"] == "done")
    ct, _ = fund(client, client.get(f"/api/c/{ct}").json())
    gate.clear()
    w = submit(client, d, "Done, looks great.", [("wall.png", png(14))]).json()
    svc = app.state.svc
    sub_id = w["submissions"][-1]["id"]
    deal = svc.db.deal_by("id", w["id"])
    good = MockAgent().verify_proof(deal, "Done, looks great.", [("image/png", png(14))])
    good["agent"] = "test"
    # race several appliers of the same verdict against each other (plus the real background review)
    threads = [threading.Thread(target=svc._apply_verdict, args=(w["id"], sub_id, dict(good))) for _ in range(5)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join()
    wait_for(lambda: client.get(f"/api/c/{ct}").json()["status"] == "paid")
    time.sleep(0.2)  # let the real background review finish too
    assert len(captures) == 1
    assert [e["kind"] for e in client.get(f"/api/c/{ct}").json()["events"]].count("captured") == 1


def test_capture_failure_during_auto_release_goes_to_review(settings, png):
    gw = MockPayPal()
    from app.paypal import PayPalError

    def boom(*a, **k):
        raise PayPalError("INTERNAL_SERVER_ERROR", 500)

    gw.capture_authorization = boom
    client, _ = make_client(settings, FakeGemini(), gateway=gw, background="inline")
    d = create(client)
    ct, _ = fund(client, d)
    w = submit(client, d, "Two coats done, all clean.", [("wall.png", png(15))]).json()
    assert w["status"] == "needs_review"
    assert any("Automatic release failed" in e["message"] for e in w["events"])


def test_reject_and_suspicious_rules_still_apply_async(settings, png):
    client, _ = make_client(settings, FakeGemini(), background="inline")
    d = create(client)
    fund(client, d)
    assert submit(client, d, "Half done, ran out of paint", [("a.png", png(16))]).json()["status"] == "changes_requested"
    w = submit(client, d, "Done!", [("white-wall-stock-photo.png", png(17))]).json()
    assert w["status"] == "needs_review" and w["submissions"][-1]["verdict"]["suspicious"] is True


def test_restart_recovers_interrupted_review_safely(settings, png):
    gate = threading.Event()
    gw = MockPayPal()
    client, _ = make_client(settings, FakeGemini(gate=gate), gateway=gw)
    gate.set()
    d = create(client)
    ct = token_of(d["client_url"])
    wait_for(lambda: client.get(f"/api/c/{ct}").json()["ai_status"] == "done")
    ct, funded = fund(client, client.get(f"/api/c/{ct}").json())
    gate.clear()
    w = submit(client, d, "Done", [("a.png", png(18))]).json()
    assert w["status"] == "verifying"
    d2 = create(client, "Sam will paint the garden fence white for $30 by Sunday")  # refinement in flight
    # "restart": a new app on the same database while the old background work is still stuck
    client2, _ = make_client(settings, FakeGemini(gate=threading.Event()), gateway=gw)
    c = client2.get(f"/api/c/{ct}").json()
    assert c["status"] == "needs_review"
    assert c["submissions"][-1]["status"] == "done" and c["submissions"][-1]["verdict"]["agent"] == "error"
    assert gw.auths[funded["authorization_id"]]["status"] == "CREATED"
    c2 = client2.get(f"/api/c/{token_of(d2['client_url'])}").json()
    assert c2["ai_status"] == "failed" and c2["worker_name"] == "Sam"
    gate.set()  # the stale first-process review now finishes: it must not move money
    time.sleep(0.3)
    assert client2.get(f"/api/c/{ct}").json()["status"] == "needs_review"
    assert gw.auths[funded["authorization_id"]]["status"] == "CREATED"


def test_worker_view_exposes_review_state(settings, png):
    gate = threading.Event()
    client, _ = make_client(settings, FakeGemini(gate=gate))
    gate.set()
    d = create(client)
    ct = token_of(d["client_url"])
    wait_for(lambda: client.get(f"/api/c/{ct}").json()["ai_status"] == "done")
    fund(client, client.get(f"/api/c/{ct}").json())
    gate.clear()
    submit(client, d, "Done", [("a.png", png(19))])
    wv = client.get(f"/api/w/{token_of(d['worker_url'])}").json()
    assert wv["status"] == "verifying" and wv["status_label"] == "AI is reviewing the proof"
    assert wv["submissions"][-1]["status"] == "ai_reviewing"
    gate.set()
    wait_for(lambda: client.get(f"/api/w/{token_of(d['worker_url'])}").json()["status"] == "paid")


def test_mock_mode_is_still_synchronous(client, png):
    d = create(client)
    assert d["ai_status"] == "done"
    fund(client, d)
    w = submit(client, d, "Two coats of white done.", [("wall.png", png(20))]).json()
    assert w["status"] == "paid" and w["submissions"][-1]["status"] == "done"


# ------------------------------------------------------------ Gemini client
def _ok(payload):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]})


PARSED = {"worker_name": "Ravi", "task": "Paint", "amount": 40, "currency": "USD", "deadline": "Sat",
          "checklist": ["a", "b", "c"], "clarifications": []}


def test_gemini_fallback_model_after_404():
    paths = []

    def handler(req):
        paths.append(req.url.path)
        if "gemini-3.8-flash" in req.url.path:
            return httpx.Response(404, json={"error": {"message": "model not found"}})
        return _ok(PARSED)

    g = GeminiAgent("KEY", transport=httpx.MockTransport(handler), fallback_model="gemini-2.5-flash", retry_sleep=0)
    assert g.parse_deal("Ravi will paint for $40")["worker_name"] == "Ravi"
    assert paths == ["/v1beta/models/gemini-3.8-flash:generateContent",
                     "/v1beta/models/gemini-2.5-flash:generateContent"]


def test_gemini_503_retries_then_fallback_once():
    paths = []

    def handler(req):
        paths.append(req.url.path.split("/")[-1])
        return httpx.Response(503, json={"error": {"message": "overloaded"}})

    g = GeminiAgent("KEY", transport=httpx.MockTransport(handler), fallback_model="gemini-2.5-flash", retry_sleep=0)
    with pytest.raises(AIError, match="fallback|503"):
        g.parse_deal("Ravi will paint for $40")
    assert paths.count("gemini-3.8-flash:generateContent") == 3
    assert paths.count("gemini-2.5-flash:generateContent") == 1


def test_gemini_no_fallback_configured():
    def handler(req):
        return httpx.Response(404, json={"error": {"message": "model not found"}})

    g = GeminiAgent("KEY", transport=httpx.MockTransport(handler), retry_sleep=0)
    with pytest.raises(AIError, match="404"):
        g.parse_deal("Ravi will paint for $40")


def test_gemini_low_thinking_with_400_fallback():
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        if "thinkingConfig" in body["generationConfig"]:
            return httpx.Response(400, json={"error": {"message": "thinking level is not supported"}})
        return _ok(PARSED)

    g = GeminiAgent("KEY", transport=httpx.MockTransport(handler), retry_sleep=0)
    g.parse_deal("Ravi will paint for $40")
    assert bodies[0]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
    assert "thinkingConfig" not in bodies[1]["generationConfig"]
    g.parse_deal("again")  # remembered: no second 400
    assert len(bodies) == 3


def test_gemini_timeout_is_generous_and_list_models():
    def handler(req):
        if req.url.path == "/v1beta/models":
            assert req.headers["x-goog-api-key"] == "KEY"
            return httpx.Response(200, json={"models": [{"name": "models/gemini-2.5-flash"},
                                                        {"name": "models/gemini-2.5-pro"},
                                                        {"name": "models/gemini-3.8-flash"}]})
        return _ok(PARSED)

    g = GeminiAgent("KEY", transport=httpx.MockTransport(handler), timeout=150)
    assert g.timeout == 150
    assert g.list_flash_models() == ["gemini-2.5-flash", "gemini-3.8-flash"]
