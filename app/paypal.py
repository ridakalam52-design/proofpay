"""PayPal Orders v2 + Payments v2 client (authorize -> capture / void).

`PayPalClient` talks to the real REST API (sandbox by default).
`MockPayPal` mimics the same interface so the app runs with no credentials.
"""
from __future__ import annotations

import time
import uuid
from typing import Protocol

import httpx


class PayPalError(Exception):
    def __init__(self, message: str, status: int | None = None, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.details = details or {}


class PaymentGateway(Protocol):
    mode: str

    def create_order(self, deal_id: str, amount: str, currency: str, description: str) -> dict: ...
    def authorize_order(self, order_id: str) -> dict: ...
    def capture_authorization(self, authorization_id: str, amount: str, currency: str, note: str) -> dict: ...
    def void_authorization(self, authorization_id: str) -> dict: ...


class PayPalClient:
    mode = "sandbox"

    def __init__(self, client_id: str, client_secret: str, base_url: str, transport: httpx.BaseTransport | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.mode = "sandbox" if "sandbox" in self.base_url else "live"
        self._http = httpx.Client(base_url=self.base_url, timeout=30.0, transport=transport)
        self._token: str | None = None
        self._token_exp = 0.0

    # -- auth --------------------------------------------------------------
    def _access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = self._http.post(
            "/v1/oauth2/token",
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
        )
        if r.status_code != 200:
            raise PayPalError("PayPal authentication failed - check PAYPAL_CLIENT_ID/SECRET", r.status_code, _safe_json(r))
        body = r.json()
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 3000))
        return self._token

    def _request(self, method: str, path: str, json: dict | None = None, request_id: str | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        if request_id:  # idempotency: retries never double-charge
            headers["PayPal-Request-Id"] = request_id
        r = self._http.request(method, path, json=json, headers=headers)
        if r.status_code >= 400:
            body = _safe_json(r)
            msg = body.get("message") or body.get("name") or f"PayPal error {r.status_code}"
            issue = (body.get("details") or [{}])[0].get("issue")
            raise PayPalError(f"{msg}{f' ({issue})' if issue else ''}", r.status_code, body)
        return _safe_json(r)

    # -- API ---------------------------------------------------------------
    def create_order(self, deal_id: str, amount: str, currency: str, description: str) -> dict:
        body = {
            "intent": "AUTHORIZE",
            "purchase_units": [
                {
                    "reference_id": deal_id,
                    "custom_id": deal_id,
                    "description": description[:127],
                    "amount": {"currency_code": currency, "value": amount},
                }
            ],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "brand_name": "ProofPay",
                        "shipping_preference": "NO_SHIPPING",
                        "user_action": "CONTINUE",
                    }
                }
            },
        }
        order = self._request("POST", "/v2/checkout/orders", body, request_id=f"order-{deal_id}-{uuid.uuid4().hex[:8]}")
        return {"id": order["id"], "status": order.get("status"), "raw": order}

    def authorize_order(self, order_id: str) -> dict:
        order = self._request("POST", f"/v2/checkout/orders/{order_id}/authorize", {}, request_id=f"auth-{order_id}")
        try:
            auth = order["purchase_units"][0]["payments"]["authorizations"][0]
        except (KeyError, IndexError):
            raise PayPalError("Order authorized but no authorization id returned", details=order)
        payer = order.get("payer", {})
        return {
            "order_id": order["id"],
            "order_status": order.get("status"),
            "authorization_id": auth["id"],
            "authorization_status": auth.get("status"),
            "expiration_time": auth.get("expiration_time"),
            "payer_email": payer.get("email_address"),
            "raw": order,
        }

    def capture_authorization(self, authorization_id: str, amount: str, currency: str, note: str) -> dict:
        body = {
            "amount": {"currency_code": currency, "value": amount},
            "final_capture": True,
            "note_to_payer": note[:255],
        }
        cap = self._request(
            "POST", f"/v2/payments/authorizations/{authorization_id}/capture", body,
            request_id=f"capture-{authorization_id}",
        )
        return {"capture_id": cap.get("id"), "status": cap.get("status"), "raw": cap}

    def void_authorization(self, authorization_id: str) -> dict:
        self._request("POST", f"/v2/payments/authorizations/{authorization_id}/void", None,
                      request_id=f"void-{authorization_id}")
        return {"status": "VOIDED"}


class MockPayPal:
    """In-memory stand-in with the same state machine as PayPal."""

    mode = "mock"

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.auths: dict[str, dict] = {}

    def create_order(self, deal_id: str, amount: str, currency: str, description: str) -> dict:
        oid = "MOCK-ORDER-" + uuid.uuid4().hex[:12].upper()
        self.orders[oid] = {"status": "CREATED", "amount": amount, "currency": currency, "deal_id": deal_id}
        return {"id": oid, "status": "CREATED", "raw": {"mock": True}}

    def authorize_order(self, order_id: str) -> dict:
        order = self.orders.get(order_id)
        if not order:
            raise PayPalError("Order not found", 404)
        if order["status"] == "COMPLETED":
            raise PayPalError("Order already authorized (ORDER_ALREADY_AUTHORIZED)", 422)
        aid = "MOCK-AUTH-" + uuid.uuid4().hex[:12].upper()
        order["status"] = "COMPLETED"
        self.auths[aid] = {**order, "status": "CREATED"}
        return {
            "order_id": order_id,
            "order_status": "COMPLETED",
            "authorization_id": aid,
            "authorization_status": "CREATED",
            "expiration_time": None,
            "payer_email": "sb-buyer@personal.example.com",
            "raw": {"mock": True},
        }

    def capture_authorization(self, authorization_id: str, amount: str, currency: str, note: str) -> dict:
        auth = self.auths.get(authorization_id)
        if auth is None:
            # Survive app restarts in mock mode: treat unknown mock auths as valid.
            auth = self.auths.setdefault(authorization_id, {"status": "CREATED"})
        if auth["status"] == "VOIDED":
            raise PayPalError("Authorization has been voided (AUTHORIZATION_VOIDED)", 422)
        if auth["status"] == "CAPTURED":
            raise PayPalError("Authorization already captured (AUTHORIZATION_ALREADY_CAPTURED)", 422)
        auth["status"] = "CAPTURED"
        return {"capture_id": "MOCK-CAP-" + uuid.uuid4().hex[:12].upper(), "status": "COMPLETED", "raw": {"mock": True}}

    def void_authorization(self, authorization_id: str) -> dict:
        auth = self.auths.setdefault(authorization_id, {"status": "CREATED"})
        if auth["status"] == "CAPTURED":
            raise PayPalError("Authorization already captured (CANNOT_BE_VOIDED)", 422)
        auth["status"] = "VOIDED"
        return {"status": "VOIDED"}


def _safe_json(r: httpx.Response) -> dict:
    try:
        data = r.json()
        return data if isinstance(data, dict) else {"data": data}
    except ValueError:
        return {"text": r.text[:500]}
