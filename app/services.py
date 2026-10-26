r"""ProofPay deal lifecycle - the escrow state machine.

    draft --fund--> funded --proof--> verifying --AI--> paid (auto-capture)
                                         |          \-> changes_requested --proof--> verifying ...
                                         |          \-> needs_review --client--> paid | changes_requested | voided
    funded / changes_requested / needs_review --client void--> voided
    funded / changes_requested / needs_review --client release--> paid

AI is asynchronous when a real model is configured (Gemini can take minutes on free infra):
  * create_deal answers at once with a rule-based draft (ai_status="refining") and refines it with
    Gemini in the background; the refinement is applied only if the deal is still an unedited draft.
  * submit_proof answers at once (deal "verifying", submission "ai_reviewing"); the verdict and the
    money decision are applied in the background under a lock + compare-and-set, so a deal can never
    be captured twice and an AI error never pays out (it goes to client review).
With the deterministic MockAgent everything runs inline, so mock mode behaves exactly as before.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import uuid
from datetime import date
from pathlib import Path

from .ai import AIError, MockAgent, money
from .config import Settings
from .db import DB, now_iso
from .paypal import PayPalError

log = logging.getLogger("proofpay")

RELEASABLE = ("funded", "changes_requested", "needs_review")
SUBMITTABLE = ("funded", "changes_requested")
ALLOWED_MIME = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/heic": "heic",
                "image/heif": "heif"}

STATUS_LABELS = {
    "draft": "Draft - not funded yet",
    "funded": "Funded - waiting for proof",
    "verifying": "AI is reviewing the proof",
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
        self._decision_lock = threading.Lock()
        Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
        self.recover_interrupted()

    # ------------------------------------------------------------ background
    @property
    def ai_is_mock(self) -> bool:
        return getattr(self.agent, "mode", "") == "mock"

    def _spawn(self, fn, *args) -> None:
        """Run slow AI work off the request. Inline for the mock agent or when AI_BACKGROUND=inline."""
        def guarded():
            try:
                fn(*args)
            except Exception:  # never let a background crash go silent
                log.exception("ProofPay background task %s failed", getattr(fn, "__name__", fn))

        if self.ai_is_mock or self.s.ai_background == "inline":
            guarded()
        else:
            threading.Thread(target=guarded, name=f"proofpay-{getattr(fn, '__name__', 'ai')}", daemon=True).start()

    def recover_interrupted(self) -> None:
        """Background work dies with the process (e.g. a free instance restarting). Fail safe on startup:
        unfinished proof reviews go to client review (never auto-pay); unfinished refinements keep the draft."""
        for sub in self.db.submissions_with_status("ai_reviewing"):
            deal = self.db.deal_by("id", sub["deal_id"])
            if not deal:
                continue
            verdict = self._error_verdict(deal["checklist"], "the server restarted before the AI review finished")
            if self.db.claim_submission(sub["id"], "ai_reviewing", "done"):
                self.db.update_submission(sub["id"], verdict, "needs_review")
                if self.db.transition(deal["id"], ("verifying",), "needs_review"):
                    self.db.log(deal["id"], "system", "warning",
                                "AI review was interrupted by a server restart - asked the client to review manually.")
        for deal in self.db.deals_with_ai_status("refining"):
            self.db.update_deal(deal["id"], ai_status="failed")
            self.db.log(deal["id"], "system", "warning",
                        "AI refinement was interrupted by a server restart; kept the rule-based draft. Please review the fields.")

    # ------------------------------------------------------------------ deals
    def create_deal(self, description: str) -> dict:
        description = (description or "").strip()
        if len(description) < 8:
            raise FlowError("Describe the deal in a sentence, e.g. 'Ravi will paint my balcony wall white for $40 by Saturday'.")
        if len(description) > 2000:
            raise FlowError("Description is too long (max 2000 characters).")
        note = None
        today = date.today().strftime("%A %Y-%m-%d")
        refine = not self.ai_is_mock
        if refine:
            # Instant rule-based draft; the real model refines it in the background.
            parsed = self.fallback_agent.parse_deal(description)
            ai_mode, ai_status = "rule-based draft", "refining"
        else:
            try:
                parsed = self.agent.parse_deal(description, today=today)
                ai_mode = self.agent.mode
            except AIError as e:
                parsed = self.fallback_agent.parse_deal(description)
                ai_mode = "mock-fallback"
                note = f"AI parser unavailable ({e}); used rule-based fallback. Please review the fields."
            ai_status = "done"
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
            "clarifications": parsed.get("clarifications", []),
            "status": "draft",
            "ai_mode": ai_mode,
            "ai_status": ai_status,
            "draft_version": 0,
            "paypal_mode": self.pp.mode,
            "created_at": ts,
            "updated_at": ts,
        }
        self.db.insert_deal(deal)
        self.db.log(deal_id, "client", "created", "Deal described in plain language", {"description": description})
        summary = (f"{parsed['worker_name']} - {parsed['task']} - ${parsed['amount']} - due {parsed['deadline']} - "
                   f"{len(parsed['checklist'])} proof criteria")
        if refine:
            self.db.log(deal_id, "ai", "parsed", f"Quick draft (rule-based): {summary}. Gemini is refining it in the background.",
                        {"parsed": parsed, "agent": ai_mode})
        else:
            self.db.log(deal_id, "ai", "parsed", f"AI structured the deal: {summary}", {"parsed": parsed, "agent": ai_mode})
        if note:
            self.db.log(deal_id, "system", "warning", note)
        if refine:
            self._spawn(self._refine_deal, deal_id, description, today, 0)
        out = self.db.deal_by("id", deal_id)
        out["clarifications"] = out.get("clarifications") or []
        return out

    def _refine_deal(self, deal_id: str, description: str, today: str, version: int) -> None:
        """Background: real-model parse. Applied only if the deal is still a draft the client hasn't edited."""
        try:
            parsed = self.agent.parse_deal(description, today=today)
        except Exception as e:  # AIError or anything unexpected: keep the rule-based draft
            self.db.update_deal(deal_id, ai_status="failed", ai_mode="mock-fallback")
            self.db.log(deal_id, "system", "warning",
                        f"AI parser unavailable ({e}); kept the rule-based draft. Please review the fields.")
            log.warning("ProofPay %s: AI refine failed: %s", deal_id, e)
            return
        summary = (f"{parsed['worker_name']} - {parsed['task']} - ${parsed['amount']} - due {parsed['deadline']} - "
                   f"{len(parsed['checklist'])} proof criteria")
        applied = self.db.apply_refinement(
            deal_id, version,
            worker_name=parsed["worker_name"], task=parsed["task"], amount=parsed["amount"],
            currency=parsed["currency"], deadline=parsed["deadline"], checklist=parsed["checklist"],
            clarifications=parsed.get("clarifications", []), ai_mode=self.agent.mode, ai_status="done",
        )
        if applied:
            self.db.log(deal_id, "ai", "refined", f"AI refined the deal: {summary}",
                        {"parsed": parsed, "agent": self.agent.mode})
        else:
            self.db.update_deal(deal_id, ai_status="done")
            self.db.log(deal_id, "ai", "refine_skipped",
                        "AI finished refining, but the terms were already edited or funded - kept them unchanged.",
                        {"parsed": parsed, "agent": self.agent.mode})
            log.info("ProofPay %s: AI refinement arrived after edit/funding; not applied", deal_id)

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
            if not self.db.edit_draft(deal["id"], **upd):
                raise FlowError("Deal terms are locked once funded.", 409)
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
                "photo_hashes": hashes, "verdict": None, "decision": None, "status": "ai_reviewing",
                "created_at": now_iso(),
            })
            self.db.log(deal["id"], "worker", "proof_submitted",
                        f"{deal['worker_name']} submitted {len(files)} photo(s)" + (f": \u201c{note[:120]}\u201d" if note else ""),
                        {"submission_id": sub_id})
            for sig in signals:
                self.db.log(deal["id"], "system", "fraud_signal", sig)
            if not self.ai_is_mock:
                self.db.log(deal["id"], "ai", "reviewing", "AI is reviewing the proof in the background.",
                            {"submission_id": sub_id})
        except Exception:
            self.db.transition(deal["id"], ("verifying",), prev_status)
            raise

        snapshot = {k: deal[k] for k in ("id", "worker_name", "task", "amount", "currency", "deadline", "checklist")}
        self._spawn(self._review_submission, snapshot, sub_id, note or "",
                    [(mime, blob) for _, mime, blob in files], signals, [n for n, _, _ in files])
        return self.db.deal_by("id", deal["id"])

    @staticmethod
    def _error_verdict(checklist: list[str], why: str) -> dict:
        return {
            "items": [{"criterion": c, "passed": False, "confidence": 0.0,
                       "reasoning": "AI verification unavailable."} for c in checklist],
            "overall": "uncertain", "confidence": 0.0, "suspicious": False, "suspicion_reasons": [],
            "summary": f"AI verification failed ({why}). The client will review manually.",
            "feedback_to_worker": "Your proof was received and is waiting for the client to review it.",
            "agent": "error",
        }

    def _review_submission(self, deal: dict, sub_id: str, note: str, photos: list[tuple[str, bytes]],
                           signals: list[str], filenames: list[str]) -> None:
        """Background: run the vision verifier, then apply the verdict. Any error -> client review."""
        try:
            verdict = self.agent.verify_proof(deal, note, photos, signals=signals, filenames=filenames)
            verdict["agent"] = self.agent.mode
        except Exception as e:  # AIError or anything unexpected: never auto-pay
            log.warning("ProofPay %s: AI verification failed: %s", deal["id"], e)
            verdict = self._error_verdict(deal["checklist"], str(e))
        if signals and not verdict.get("suspicious"):
            verdict["suspicious"] = True
            verdict["suspicion_reasons"] = list(verdict.get("suspicion_reasons") or []) + signals
        self._apply_verdict(deal["id"], sub_id, verdict)

    def _apply_verdict(self, deal_id: str, sub_id: str, verdict: dict) -> None:
        """Apply a verdict exactly once. The lock + submission CAS + deal-status CAS make a double capture
        impossible even if two reviews (or a review and a client action) race."""
        with self._decision_lock:
            if not self.db.claim_submission(sub_id, "ai_reviewing", "deciding"):
                log.info("ProofPay %s: verdict for %s already applied; ignoring", deal_id, sub_id)
                return
            deal = self.db.deal_by("id", deal_id)
            verdict["threshold"] = self.s.approval_threshold
            if not deal or deal["status"] != "verifying":
                self.db.update_submission(sub_id, verdict, "superseded")
                self.db.log(deal_id, "system", "warning",
                            "AI verdict arrived after the deal had moved on - no action taken.")
                return
            decision = self.decide(verdict)
            self.db.update_submission(sub_id, verdict, decision)
            passed = sum(1 for it in verdict["items"] if it["passed"])
            self.db.log(deal_id, "ai", "verdict",
                        f"AI verdict: {verdict['overall'].upper()} - {passed}/{len(verdict['items'])} criteria met, "
                        f"confidence {verdict['confidence']:.0%}" + (" - flagged as suspicious" if verdict["suspicious"] else ""),
                        {"submission_id": sub_id, "decision": decision})

            if decision == "auto_release":
                try:
                    self._capture(deal, actor="ai", reason=f"AI approved proof with {verdict['confidence']:.0%} confidence",
                                  from_statuses=("verifying",))
                except FlowError as e:
                    # capture failed (PayPal error or state moved): hand to the client, never retry silently
                    if self.db.transition(deal_id, ("verifying",), "needs_review"):
                        self.db.log(deal_id, "system", "needs_review",
                                    f"Automatic release failed ({e}) - asked the client to review.")
                return
            if decision == "changes_requested":
                self.db.transition(deal_id, ("verifying",), "changes_requested")
                self.db.log(deal_id, "ai", "changes_requested",
                            "Feedback sent to worker: " + (verdict.get("feedback_to_worker") or "")[:300])
            else:
                self.db.transition(deal_id, ("verifying",), "needs_review")
                self.db.log(deal_id, "ai", "needs_review",
                            "Not confident enough to move money automatically - asked the client to review.")

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

    def _capture(self, deal: dict, actor: str, reason: str, from_statuses: tuple[str, ...] | None = None) -> dict:
        from_statuses = from_statuses or (deal["status"],)
        if not self.db.transition(deal["id"], from_statuses, "capturing"):
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
            "ai_status": deal.get("ai_status") or "done",
            "paypal_mode": deal["paypal_mode"],
            "created_at": deal["created_at"],
            "submissions": [
                {"id": s["id"], "note": s["note"], "photos": [f"/media/{p}" for p in s["photos"]],
                 "verdict": s["verdict"], "decision": s["decision"],
                 "status": "ai_reviewing" if s.get("status") in ("ai_reviewing", "deciding") else (s.get("status") or "done"),
                 "created_at": s["created_at"]}
                for s in subs
            ],
            "events": [{k: e[k] for k in ("ts", "actor", "kind", "message")} for e in events],
            "worker_url": f"/w/{deal['worker_token']}",
            "can": {},
        }
        st = deal["status"]
        if role == "client":
            out["client_url"] = f"/c/{deal['client_token']}"
            out["clarifications"] = deal.get("clarifications") or []
            out["can"] = {"edit": st == "draft", "fund": st == "draft", "release": st in RELEASABLE,
                          "void": st in RELEASABLE, "request_changes": st in ("needs_review", "funded")}
        else:
            out["can"] = {"submit": st in SUBMITTABLE}
            out.pop("paypal_order_id")
        return out
