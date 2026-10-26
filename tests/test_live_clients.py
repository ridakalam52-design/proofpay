"""Exercise the REAL PayPal and Gemini clients against fake HTTP transports.

These verify the exact REST calls (paths, headers, bodies) the live sandbox mode
makes, without needing network access or keys.
"""
import json

import httpx
import pytest

from app.ai import AIError, GeminiAgent
from app.paypal import PayPalClient, PayPalError


def paypal_transport(calls):
    def handler(req: httpx.Request):
        calls.append(req)
        p = req.url.path
        if p == "/v1/oauth2/token":
            assert req.headers["authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "TOKEN", "expires_in": 32000})
        assert req.headers["authorization"] == "Bearer TOKEN"
        if p == "/v2/checkout/orders":
            body = json.loads(req.content)
            assert body["intent"] == "AUTHORIZE"
            assert body["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "40.00"}
            return httpx.Response(201, json={"id": "5O190127TN364715T", "status": "CREATED"})
        if p == "/v2/checkout/orders/5O190127TN364715T/authorize":
            return httpx.Response(201, json={
                "id": "5O190127TN364715T", "status": "COMPLETED",
                "payer": {"email_address": "buyer@example.com"},
                "purchase_units": [{"payments": {"authorizations": [
                    {"id": "0VF52814937998046", "status": "CREATED", "expiration_time": "2026-11-26T00:00:00Z"}]}}]})
        if p == "/v2/payments/authorizations/0VF52814937998046/capture":
            assert req.headers["paypal-request-id"] == "capture-0VF52814937998046"
            assert json.loads(req.content)["final_capture"] is True
            return httpx.Response(201, json={"id": "2GG279541U471931P", "status": "COMPLETED"})
        if p == "/v2/payments/authorizations/0VF52814937998046/void":
            return httpx.Response(204)
        if p == "/v2/payments/authorizations/BAD/void":
            return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "message": "The requested action could not be performed.",
                                             "details": [{"issue": "PREVIOUSLY_CAPTURED"}]})
        return httpx.Response(404, json={"message": "not found"})
    return httpx.MockTransport(handler)


def test_paypal_client_authorize_capture_void():
    calls = []
    pp = PayPalClient("id", "secret", "https://api-m.sandbox.paypal.com", transport=paypal_transport(calls))
    assert pp.mode == "sandbox"
    order = pp.create_order("PP-1", "40.00", "USD", "Paint wall")
    assert order["id"] == "5O190127TN364715T"
    auth = pp.authorize_order(order["id"])
    assert auth["authorization_id"] == "0VF52814937998046"
    cap = pp.capture_authorization(auth["authorization_id"], "40.00", "USD", "done")
    assert cap["capture_id"] == "2GG279541U471931P" and cap["status"] == "COMPLETED"
    assert pp.void_authorization(auth["authorization_id"])["status"] == "VOIDED"
    with pytest.raises(PayPalError, match="PREVIOUSLY_CAPTURED"):
        pp.void_authorization("BAD")
    # token fetched once and cached
    assert sum(1 for c in calls if c.url.path == "/v1/oauth2/token") == 1


def gemini_transport(payload, status=200, seen=None):
    def handler(req: httpx.Request):
        if seen is not None:
            seen.append(req)
        assert req.url.path == "/v1beta/models/gemini-2.5-flash:generateContent"
        assert req.headers["x-goog-api-key"] == "KEY"
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "API key not valid"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]})
    return httpx.MockTransport(handler)


def test_gemini_parse_deal():
    seen = []
    g = GeminiAgent("KEY", transport=gemini_transport({
        "worker_name": "Ravi", "task": "Paint the balcony wall white", "amount": 40, "currency": "USD",
        "deadline": "Saturday (2026-10-31)", "checklist": ["Wall fully white", "No drips", "Floor clean"],
        "clarifications": []}, seen=seen))
    d = g.parse_deal("Ravi will paint my balcony wall white for $40 by Saturday")
    assert d["amount"] == "40.00" and d["worker_name"] == "Ravi" and len(d["checklist"]) == 3
    body = json.loads(seen[0].content)
    assert body["generationConfig"]["responseMimeType"] == "application/json"


def test_gemini_verify_sends_images_and_normalizes():
    seen = []
    g = GeminiAgent("KEY", transport=gemini_transport({
        "items": [{"criterion": "x", "passed": True, "confidence": 0.9, "reasoning": "visible"}],
        "overall": "approve", "confidence": 0.91, "summary": "ok", "feedback_to_worker": "thanks",
        "suspicious": False, "suspicion_reasons": []}, seen=seen))
    v = g.verify_proof({"checklist": ["Wall fully white"], "worker_name": "Ravi", "task": "t", "amount": "40.00",
                        "currency": "USD", "deadline": "Sat"}, "done", [("image/png", b"\x89PNG")])
    assert v["overall"] == "approve" and v["items"][0]["criterion"] == "Wall fully white"
    parts = json.loads(seen[0].content)["contents"][0]["parts"]
    assert parts[1]["inline_data"]["mime_type"] == "image/png"


def test_gemini_error_raises():
    g = GeminiAgent("KEY", transport=gemini_transport({}, status=400))
    with pytest.raises(AIError, match="API key not valid"):
        g.parse_deal("Ravi will paint my wall for $40")
