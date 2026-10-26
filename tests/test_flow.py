"""End-to-end escrow flow in MOCK mode: parse -> authorize -> verify -> capture / void."""
from app.ai import MockAgent, normalize_verdict

DESC = "Ravi will paint my balcony wall white for $40 by Saturday"


def create(client, desc=DESC):
    r = client.post("/api/deals", json={"description": desc})
    assert r.status_code == 200, r.text
    return r.json()


def token_of(url):
    return url.rsplit("/", 1)[-1]


def fund(client, deal):
    ct = token_of(deal["client_url"])
    order = client.post(f"/api/c/{ct}/paypal/order").json()
    assert order["id"].startswith("MOCK-ORDER-")
    r = client.post(f"/api/c/{ct}/paypal/authorize", json={"orderID": order["id"]})
    assert r.status_code == 200, r.text
    return ct, r.json()


def submit(client, deal, note, photos):
    wt = token_of(deal["worker_url"])
    files = [("photos", (name, blob, "image/png")) for name, blob in photos]
    return client.post(f"/api/w/{wt}/proof", data={"note": note}, files=files)


# ---------------------------------------------------------------- parsing
def test_parse_extracts_structured_deal(client):
    d = create(client)
    assert d["worker_name"] == "Ravi"
    assert d["amount"] == "40.00"
    assert d["currency"] == "USD"
    assert d["deadline"].lower().startswith("saturday")
    assert "paint" in d["task"].lower()
    assert len(d["checklist"]) >= 3
    assert any("white" in c.lower() for c in d["checklist"])
    assert d["status"] == "draft"
    kinds = [e["kind"] for e in d["events"]]
    assert kinds[:2] == ["created", "parsed"]


def test_parse_rejects_empty(client):
    r = client.post("/api/deals", json={"description": "hi"})
    assert r.status_code == 400


def test_edit_terms_then_locked_after_funding(client):
    d = create(client)
    ct = token_of(d["client_url"])
    r = client.patch(f"/api/c/{ct}", json={"amount": "45", "checklist": ["Wall is white", "Floor is clean"]})
    assert r.status_code == 200
    assert r.json()["amount"] == "45.00" and r.json()["checklist"] == ["Wall is white", "Floor is clean"]
    fund(client, r.json())
    assert client.patch(f"/api/c/{ct}", json={"amount": "1"}).status_code == 409


# ---------------------------------------------------------- happy path
def test_full_flow_auto_capture(client, png, gateway):
    d = create(client)
    ct, funded = fund(client, d)
    assert funded["status"] == "funded"
    assert funded["authorization_id"].startswith("MOCK-AUTH-")
    assert gateway.auths[funded["authorization_id"]]["status"] == "CREATED"  # held, not captured

    r = submit(client, d, "Two coats of white done, edges taped, floor cleaned.", [("wall.png", png(1))])
    assert r.status_code == 200, r.text
    w = r.json()
    assert w["status"] == "paid"
    v = w["submissions"][-1]["verdict"]
    assert v["overall"] == "approve" and v["confidence"] >= 0.75
    assert all(it["passed"] for it in v["items"])
    assert len(v["items"]) == len(d["checklist"])

    c = client.get(f"/api/c/{ct}").json()
    assert c["capture_id"].startswith("MOCK-CAP-")
    assert gateway.auths[funded["authorization_id"]]["status"] == "CAPTURED"
    kinds = [e["kind"] for e in c["events"]]
    for k in ["created", "parsed", "order_created", "authorized", "proof_submitted", "verdict", "captured"]:
        assert k in kinds
    # photo is served back
    assert client.get(w["submissions"][-1]["photos"][0]).status_code == 200
    # no double capture
    assert client.post(f"/api/c/{ct}/release").status_code == 409
    assert client.post(f"/api/c/{ct}/void").status_code == 409


# ------------------------------------------------------ reject & resubmit
def test_reject_feedback_then_resubmit(client, png):
    d = create(client)
    ct, _ = fund(client, d)
    r = submit(client, d, "Half done, ran out of paint", [("wall.png", png(2))])
    w = r.json()
    assert w["status"] == "changes_requested"
    v = w["submissions"][-1]["verdict"]
    assert v["overall"] == "reject"
    assert any(not it["passed"] for it in v["items"])
    assert "re-photograph" in v["feedback_to_worker"].lower()

    r2 = submit(client, d, "Finished the second coat, all clean now.", [("wall2.png", png(3))])
    assert r2.json()["status"] == "paid"
    assert len(r2.json()["submissions"]) == 2


# ------------------------------------------------- suspicious / review
def test_suspicious_stock_photo_goes_to_client_review(client, png):
    d = create(client)
    ct, _ = fund(client, d)
    w = submit(client, d, "Done!", [("white-wall-stock-photo.png", png(4))]).json()
    assert w["status"] == "needs_review"
    v = w["submissions"][-1]["verdict"]
    assert v["suspicious"] is True and v["suspicion_reasons"]
    # client overrides and releases
    r = client.post(f"/api/c/{ct}/release")
    assert r.status_code == 200 and r.json()["status"] == "paid"
    assert any(e["kind"] == "release_decision" and "override" in e["message"] for e in r.json()["events"])


def test_reused_photo_across_deals_is_flagged(client, png):
    a = create(client)
    fund(client, a)
    assert submit(client, a, "Done, looks great.", [("a.png", png(5))]).json()["status"] == "paid"
    b = create(client, "Sam will paint the garden fence white for $30 by Sunday")
    ctb, _ = fund(client, b)
    w = submit(client, b, "Done, looks great.", [("b.png", png(5))]).json()
    assert w["status"] == "needs_review"
    assert any("different deal" in r for r in w["submissions"][-1]["verdict"]["suspicion_reasons"])
    # client asks for changes -> worker can resubmit
    r = client.post(f"/api/c/{ctb}/request-changes", json={"note": "Please send a fresh photo"})
    assert r.json()["status"] == "changes_requested"
    assert submit(client, b, "Fresh photo of the fence.", [("c.png", png(6))]).json()["status"] == "paid"


# ------------------------------------------------------------ void
def test_client_void_releases_hold(client, png, gateway):
    d = create(client)
    ct, funded = fund(client, d)
    r = client.post(f"/api/c/{ct}/void")
    assert r.status_code == 200 and r.json()["status"] == "voided"
    assert gateway.auths[funded["authorization_id"]]["status"] == "VOIDED"
    # worker can no longer submit, client can't capture
    assert submit(client, d, "done", [("x.png", png(7))]).status_code == 409
    assert client.post(f"/api/c/{ct}/release").status_code == 409


# ------------------------------------------------------ guard rails
def test_cannot_submit_before_funding_or_without_photo(client, png):
    d = create(client)
    assert submit(client, d, "done", [("x.png", png(8))]).status_code == 409
    fund(client, d)
    wt = token_of(d["worker_url"])
    assert client.post(f"/api/w/{wt}/proof", data={"note": "done"}).status_code == 400
    bad = client.post(f"/api/w/{wt}/proof", data={"note": "x"}, files=[("photos", ("a.txt", b"hello", "text/plain"))])
    assert bad.status_code == 400


def test_worker_link_cannot_move_money(client):
    d = create(client)
    wt = token_of(d["worker_url"])
    assert client.post(f"/api/c/{wt}/release").status_code == 404
    assert client.get(f"/api/c/{wt}").status_code == 404
    view = client.get(f"/api/w/{wt}").json()
    assert "client_url" not in view and view["role"] == "worker"


def test_cannot_fund_twice_or_with_foreign_order(client):
    d = create(client)
    ct, _ = fund(client, d)
    assert client.post(f"/api/c/{ct}/paypal/order").status_code == 409
    d2 = create(client)
    ct2 = token_of(d2["client_url"])
    client.post(f"/api/c/{ct2}/paypal/order")
    assert client.post(f"/api/c/{ct2}/paypal/authorize", json={"orderID": "MOCK-ORDER-OTHER"}).status_code == 400


def test_pages_render(client):
    d = create(client)
    assert client.get("/").status_code == 200
    assert "ProofPay" in client.get(d["client_url"]).text
    assert client.get(d["worker_url"]).status_code == 200
    assert client.get("/c/nope").status_code == 404
    assert client.get("/healthz").json()["ai"] == "mock"


# ---------------------------------------------------- AI safety net
def test_verdict_normalization_is_conservative():
    v = normalize_verdict({"items": [{"passed": True, "confidence": 95}, {"passed": False, "confidence": 0.9}],
                           "overall": "approve", "confidence": 1.7}, ["a", "b"])
    assert v["overall"] == "uncertain"  # model said approve but an item failed
    assert v["items"][0]["confidence"] == 0.95
    assert 0 <= v["confidence"] <= 1


def test_mock_agent_variants():
    m = MockAgent()
    tutor = m.parse_deal("Ananya will tutor my son in algebra for $25 by Friday")
    assert tutor["worker_name"] == "Ananya" and tutor["amount"] == "25.00"
    deliv = m.parse_deal("Marco will deliver the cake to 12 Lake Road for 15 dollars by 6pm")
    assert deliv["amount"] == "15.00" and any("drop-off" in c for c in deliv["checklist"])
