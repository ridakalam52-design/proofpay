r"""ProofPay deal lifecycle - the escrow state machine.

    draft --fund--> funded --proof--> verifying --AI--> paid (auto-capture)
                                         |          \-> changes_requested --proof--> verifying ...
                                         |          \-> needs_review --client--> paid | changes_requested | voided
    funded / changes_requested / needs_review --client void--> voided
    funded / changes_requested / needs_review --client release--> paid
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import date
from pathlib import Path

from .ai import AIError, MockAgent, money
from .config import Settings
from .db import DB, now_iso
from .paypal import PayPalError

RELEASABLE = ("funded", "changes_requested", "needs_review")
SUBMITTABLE = ("funded", "changes_requested")
ALLOWED_MIME = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/heic": "heic",
                "image/heif": "heif"}

STATUS_LABELS = {
    "draft": "Draft - not funded yet",
    "funded": "Funded - waiting for proof",
    "verifying": "AI is checking the proof",
    "changes_requested": "Changes requested",
    "needs_review": "Needs client review",
    "capturing": "Releasing payment",
    "voiding": "Cancelling hold",
    "paid": "Paid - work proven",
    "voided": "Cancelled - hold released",
}


class FlowError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class ProofPay:
    def __init__(self, settings: Settings, db: DB, agent, gateway):
        self.s = settings
        self.db = db
        self.agent = agent
        self.pp = gateway
        self.fallback_agent = MockAgent()
        Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ deals
    def create_deal(self, description: str) -> dict:
        description = (description or "").strip()
        if len(description) < 8:
            raise FlowError("Describe the deal in a sentence, e.g. 'Ravi will paint my balcony wall white for $40 by Saturday'.")
        if len(description) > 2000:
            raise FlowError("Description is too long (max 2000 characters).")
        note = None
        try:
            parsed = self.agent.parse_deal(description, today=date.today().strftime("%A %Y-%m-%d"))
            ai_mode = self.agent.mode
        except AIError as e:
            parsed = self.fallback_agent.parse_deal(description)
            ai_mode = "mock-fallback"
            note = f"AI parser unavailable ({e}); used rule-based fallback. Please review the fields."
        deal_id = "PP-" + secrets.token_hex(3).upper()
        ts = now_iso()
        deal = {
            "id": deal_id,
            "client_token": secrets.token_urlsafe(16),
            "worker_token": secrets.token_urlsafe(16),
            "description": description,
            "worker_name": parsed["worker_name"],
            "task": parsed["task"],
            "amount": parsed["amount"],
            "currency": parsed["currency"],
            "deadline": parsed["deadline"],
            "checklist": parsed["checklist"],
            "status": "draft",
            "ai_mode": ai_mode,
            "paypal_mode": self.pp.mode,
            "created_at": ts,
            "updated_at": ts,
        }
        self.db.insert_deal(deal)
        self.db.log(deal_id, "client", "created", "Deal described in plain language", {"description": description})
        self.db.log(deal_id, "ai", "parsed",
                    f"AI structured the deal: {parsed['worker_name']} - {parsed['task']} - "
                    f"${parsed['amount']} - due {parsed['deadline']} - {len(parsed['checklist'])} proof criteria",
                    {"parsed": parsed, "agent": ai_mode})
        if note:
            self.db.log(deal_id, "system", "warning", note)
        out = self.db.deal_by("id", deal_id)
        out["clarifications"] = parsed.get("clarifications", [])
        return out

    def update_draft(self, deal: dict, fields: dict) -> dict:
        if deal["status"] != "draft":
            raise FlowError("Deal terms are locked once funded.", 409)
        upd = {}
        for key in ("worker_name", "task", "deadline"):
            if key in fields and fields[key] is not None:
                val = str(fields[key]).strip()
                if not val:
                    raise FlowError(f"{key.replace('_', ' ').title()} cannot be empty.")
                upd[key] = val[:300]
        if "amount" in fields and fields["amount"] is not None:
            upd["amount"] = money(fields["amount"])
        if "checklist" in fields and fields["checklist"] is not None:
            items = [str(c).strip() for c in fields["checklist"] if str(c).strip()]
            if not items:
                raise FlowError("Keep at least one proof criterion.")
            upd["checklist"] = items[:8]
        if upd:
            self.db.update_deal(deal["id"], **upd)
            self.db.log(deal["id"], "client", "edited", "Client adjusted deal terms: " + ", ".join(sorted(upd)))
        return self.db.deal_by("id", deal["id"])

    # ---------------------------------------------------------------- funding
    def create_order(self, deal: dict) -> dict:
        if deal["status"] != "draft":
            raise FlowError("This deal is already funded.", 409)
        if float(deal["amount"]) < 1:
            raise FlowError("Set an amount of at least $1.00 before funding.")
        try:
            order = self.pp.create_order(deal["id"], deal["amount"], deal["currency"],
                                         f"ProofPay escrow: {deal['task']}")
        except PayPalError as e:
            self.db.log(deal["id"], "paypal", "error", f"Could not create PayPal order: {e}")
            raise FlowError(f"PayPal: {e}", 502)
        self.db.update_deal(deal["id"], paypal_order_id=order["id"])
        self.db.log(deal["id"], "paypal", "order_created",
                    f"PayPal order {order['id']} created (intent=AUTHORIZE, ${deal['amount']})")
        return {"id": order["id"]}

    def authorize(self, deal: dict, order_id: str) -> dict:
        if deal["status"] != "draft":
            raise FlowError("This deal is already funded.", 409)
        if not order_id or order_id != deal.get("paypal_order_id"):
            raise FlowError("Order does not belong to this deal.", 400)
        try:
            res = self.pp.authorize_order(order_id)
        except PayPalError as e:
            self.db.log(deal["id"], "paypal", "error", f"Authorization failed: {e}")
            raise FlowError(f"PayPal: {e}", 502)
        if not self.db.transition(deal["id"], ("draft",), "funded"):
            raise FlowError("Deal changed state during funding.", 409)
        self.db.update_deal(deal["id"], authorization_id=res["authorization_id"])
        exp = f" (hold valid until {res['expiration_time'][:10]})" if res.get("expiration_time") else ""
        self.db.log(deal["id"], "paypal", "authorized",
                    f"${deal['amount']} authorized and held - not captured{exp}. Authorization {res['authorization_id']}",
                    {"authorization_id": res["authorization_id"], "status": res.get("authorization_status")})
        return self.db.deal_by("id", deal["id"])

    # ------------------------------------------------------------------ proof
    def submit_proof(self, deal: dict, note: str, files: list[tuple[str, str, bytes]]) -> dict:
        """files: list of (filename, mime, bytes)."""
        if deal["status"] not in SUBMITTABLE:
            if deal["status"] == "draft":
                raise FlowError("The client hasn't funded this deal yet - proof can be sent once the money is held.", 409)
            raise FlowError(f"Proof can't be submitted while the deal is '{deal['status']}'.", 409)
        files = [f for f in files if f[2]]
        if not files:
            raise FlowError("Attach at least one photo of the finished work.")
        if len(files) > self.s.max_photos:
            raise FlowError(f"Up to {self.s.max_photos} photos per submission.")
        for name, mime, blob in files:
            if mime not in ALLOWED_MIME:
                raise FlowError(f"{name}: unsupported type {mime}. Use JPG, PNG, WEBP or HEIC.")
            if len(blob) > self.s.max_photo_mb * 1024 * 1024:
                raise FlowError(f"{name}: larger than {self.s.max_photo_mb:g} MB.")

        if not self.db.transition(deal["id"], SUBMITTABLE, "verifying"):
            raise FlowError("Another submission is being checked right now.", 409)
        prev_status = deal["status"]
        sub_id = uuid.uuid4().hex[:12]
        try:
            # store photos + server-side fraud signals
            folder = Path(self.s.upload_dir) / deal["id"]
            folder.mkdir(parents=True, exist_ok=True)
            stored, hashes, signals = [], [], []
            for i, (name, mime, blob) in enumerate(files):
                sha = hashlib.sha256(blob).hexdigest()
                fname = f"{sub_id}-{i}.{ALLOWED_MIME[mime]}"
                (folder / fname).write_bytes(blob)
                stored.append(f"{deal['id']}/{fname}")
                if self.db.hash_seen_elsewhere(sha, deal["id"]):
                    signals.append(f"Photo {i + 1} was already used as proof for a different deal")
                elif self.db.hash_seen_in_deal(sha, deal["id"]):
                    signals.append(f"Photo {i + 1} is identical to a photo from an earlier submission on this deal")
                hashes.append(sha)
            self.db.insert_submission({
                "id": sub_id, "deal_id": deal["id"], "note": (note or "")[:1000], "photos": stored,
                "photo_hashes": hashes, "verdict": None, "decision": None, "created_at": now_iso(),
            })
            self.db.log(deal["id"], "worker", "proof_submitted",
                        f"{deal['worker_name']} submitted {len(files)} photo(s)" + (f": \u201c{note[:120]}\u201d" if note else ""),
                        {"submission_id": sub_id})
            for sig in signals:
                self.db.log(deal["id"], "system", "fraud_signal", sig)

            try:
                verdict = self.agent.verify_proof(
                    deal, note or "", [(mime, blob) for _, mime, blob in files],
                    signals=signals, filenames=[n for n, _, _ in files],
                )
                verdict["agent"] = self.agent.mode
            except AIError as e:
                verdict = {
                    "items": [{"criterion": c, "passed": False, "confidence": 0.0,
                               "reasoning": "AI verification unavailable."} for c in deal["checklist"]],
                    "overall": "uncertain", "confidence": 0.0, "suspicious": False, "suspicion_reasons": [],
                    "summary": f"AI verification failed ({e}). The client will review manually.",
                    "feedback_to_worker": "Your proof was received and is waiting for the client to review it.",
                    "agent": "error",
                }
            if signals and not verdict.get("suspicious"):
                verdict["suspicious"] = True
                verdict["suspicion_reasons"] = list(verdict.get("suspicion_reasons") or []) + signals
        except Exception:
            self.db.transition(deal["id"], ("verifying",), prev_status)
            raise

        decision = self.decide(verdict)
        verdict["threshold"] = self.s.approval_threshold
        self.db.update_submission(sub_id, verdict, decision)
        passed = sum(1 for it in verdict["items"] if it["passed"])
        self.db.log(deal["id"], "ai", "verdict",
                    f"AI verdict: {verdict['overall'].upper()} - {passed}/{len(verdict['items'])} criteria met, "
                    f"confidence {verdict['confidence']:.0%}" + (" - flagged as suspicious" if verdict["suspicious"] else ""),
                    {"submission_id": sub_id, "decision": decision})

        if decision == "auto_release":
            self.db.transition(deal["id"], ("verifying",), "funded")
            return self._capture(self.db.deal_by("id", deal["id"]), actor="ai",
                                 reason=f"AI approved proof with {verdict['confidence']:.0%} confidence")
        if decision == "changes_requested":
            self.db.transition(deal["id"], ("verifying",), "changes_requested")
            self.db.log(deal["id"], "ai", "changes_requested",
                        "Feedback sent to worker: " + (verdict.get("feedback_to_worker") or "")[:300])
        else:
            self.db.transition(deal["id"], ("verifying",), "needs_review")
            self.db.log(deal["id"], "ai", "needs_review",
                        "Not confident enough to move money automatically - asked the client to review.")
        return self.db.deal_by("id", deal["id"])

    def decide(self, verdict: dict) -> str:
        all_pass = all(it["passed"] for it in verdict["items"])
        conf = verdict["confidence"]
        if (verdict["overall"] == "approve" and all_pass and conf >= self.s.approval_threshold
                and not verdict.get("suspicious")):
            return "auto_release"
        if verdict["overall"] == "reject" and not verdict.get("suspicious"):
            return "changes_requested"
        return "needs_review"

    # ---------------------------------------------------------- client actions
    def release(self, deal: dict) -> dict:
        if deal["status"] not in RELEASABLE:
            raise FlowError(f"Can't release payment while the deal is '{deal['status']}'.", 409)
        return self._capture(deal, actor="client", reason="Client released payment manually (override)")

    def request_changes(self, deal: dict, note: str) -> dict:
        if deal["status"] not in ("needs_review", "funded"):
            raise FlowError(f"Can't request changes while the deal is '{deal['status']}'.", 409)
        if not self.db.transition(deal["id"], (deal["status"],), "changes_requested"):
            raise FlowError("Deal changed state, refresh and try again.", 409)
        self.db.log(deal["id"], "client", "changes_requested",
                    "Client asked for changes" + (f": \u201c{note.strip()[:300]}\u201d" if note and note.strip() else ""))
        return self.db.deal_by("id", deal["id"])

    def void(self, deal: dict) -> dict:
        if deal["status"] not in RELEASABLE:
            raise FlowError(f"Can't cancel while the deal is '{deal['status']}'.", 409)
        if not self.db.transition(deal["id"], (deal["status"],), "voiding"):
            raise FlowError("Deal changed state, refresh and try again.", 409)
        try:
            self.pp.void_authorization(deal["authorization_id"])
        except PayPalError as e:
            self.db.transition(deal["id"], ("voiding",), deal["status"])
            self.db.log(deal["id"], "paypal", "error", f"Void failed: {e}")
            raise FlowError(f"PayPal: {e}", 502)
        self.db.transition(deal["id"], ("voiding",), "voided")
        self.db.log(deal["id"], "paypal", "voided",
                    f"Authorization {deal['authorization_id']} voided - ${deal['amount']} hold released back to the client")
        return self.db.deal_by("id", deal["id"])

    def _capture(self, deal: dict, actor: str, reason: str) -> dict:
        if not self.db.transition(deal["id"], (deal["status"],), "capturing"):
            raise FlowError("Deal changed state, refresh and try again.", 409)
        try:
            cap = self.pp.capture_authorization(
                deal["authorization_id"], deal["amount"], deal["currency"],
                f"ProofPay: work proven - {deal['task']}",
            )
        except PayPalError as e:
            self.db.transition(deal["id"], ("capturing",), deal["status"])
            self.db.log(deal["id"], "paypal", "error", f"Capture failed: {e}")
            raise FlowError(f"PayPal: {e}", 502)
        self.db.transition(deal["id"], ("capturing",), "paid")
        self.db.update_deal(deal["id"], capture_id=cap["capture_id"])
        self.db.log(deal["id"], actor, "release_decision", reason)
        self.db.log(deal["id"], "paypal", "captured",
                    f"${deal['amount']} captured (capture {cap['capture_id']}, status {cap['status']}) - paid to {deal['worker_name']}",
                    {"capture_id": cap["capture_id"]})
        return self.db.deal_by("id", deal["id"])

    # ------------------------------------------------------------------ views
    def view(self, deal: dict, role: str) -> dict:
        subs = self.db.submissions(deal["id"])
        events = self.db.events(deal["id"])
        out = {
            "id": deal["id"],
            "role": role,
            "description": deal["description"],
            "worker_name": deal["worker_name"],
            "task": deal["task"],
            "amount": deal["amount"],
            "currency": deal["currency"],
            "deadline": deal["deadline"],
            "checklist": deal["checklist"],
            "status": deal["status"],
            "status_label": STATUS_LABELS.get(deal["status"], deal["status"]),
            "authorization_id": deal["authorization_id"],
            "capture_id": deal["capture_id"],
            "paypal_order_id": deal["paypal_order_id"],
            "ai_mode": deal["ai_mode"],
            "paypal_mode": deal["paypal_mode"],
            "created_at": deal["created_at"],
            "submissions": [
                {"id": s["id"], "note": s["note"], "photos": [f"/media/{p}" for p in s["photos"]],
                 "verdict": s["verdict"], "decision": s["decision"], "created_at": s["created_at"]}
                for s in subs
            ],
            "events": [{k: e[k] for k in ("ts", "actor", "kind", "message")} for e in events],
            "worker_url": f"/w/{deal['worker_token']}",
            "can": {},
        }
        st = deal["status"]
        if role == "client":
            out["client_url"] = f"/c/{deal['client_token']}"
            out["can"] = {"edit": st == "draft", "fund": st == "draft", "release": st in RELEASABLE,
                          "void": st in RELEASABLE, "request_changes": st in ("needs_review", "funded")}
        else:
            out["can"] = {"submit": st in SUBMITTABLE}
            out.pop("paypal_order_id")
        return out
