"""AI agents: (1) deal parser, (2) proof-of-work vision verifier.

`GeminiAgent` calls the Google Gemini REST API with structured JSON output.
`MockAgent` is a deterministic, rule-based stand-in used when no key is set.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import date
from typing import Protocol

import httpx


class AIError(Exception):
    pass


class Agent(Protocol):
    mode: str

    def parse_deal(self, description: str, today: str | None = None) -> dict: ...
    def verify_proof(self, deal: dict, note: str, photos: list[tuple[str, bytes]]) -> dict: ...


# ---------------------------------------------------------------------------
# Schemas & prompts
# ---------------------------------------------------------------------------
DEAL_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "worker_name": {"type": "STRING", "description": "Name of the person doing the work"},
        "task": {"type": "STRING", "description": "Short imperative description of the job"},
        "amount": {"type": "NUMBER", "description": "Agreed price"},
        "currency": {"type": "STRING", "description": "ISO currency code, default USD"},
        "deadline": {"type": "STRING", "description": "Deadline as stated or resolved, e.g. 'Saturday (2026-10-31)'"},
        "checklist": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "3-5 concrete, photo-verifiable proof criteria",
        },
        "clarifications": {"type": "ARRAY", "items": {"type": "STRING"},
                           "description": "Anything ambiguous the client should double-check"},
    },
    "required": ["worker_name", "task", "amount", "currency", "deadline", "checklist"],
}

VERDICT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "criterion": {"type": "STRING"},
                    "passed": {"type": "BOOLEAN"},
                    "confidence": {"type": "NUMBER", "description": "0.0-1.0"},
                    "reasoning": {"type": "STRING"},
                },
                "required": ["criterion", "passed", "confidence", "reasoning"],
            },
        },
        "overall": {"type": "STRING", "enum": ["approve", "reject", "uncertain"]},
        "confidence": {"type": "NUMBER", "description": "0.0-1.0 overall confidence that the work is done"},
        "summary": {"type": "STRING"},
        "feedback_to_worker": {"type": "STRING",
                               "description": "Specific, polite instructions for what to fix or re-photograph"},
        "suspicious": {"type": "BOOLEAN"},
        "suspicion_reasons": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["items", "overall", "confidence", "summary", "feedback_to_worker", "suspicious"],
}

PARSE_PROMPT = """You are ProofPay's deal-structuring agent. A client describes an informal gig
deal in plain language. Extract a structured escrow deal.

Rules:
- worker_name: the person doing the work (use "Worker" if not named).
- task: one concise sentence of what must be done.
- amount: a number. currency: ISO code (default "USD"; ProofPay currently settles in USD).
- deadline: keep the client's wording and, if relative, add the resolved date in parentheses. Today is {today}.
- checklist: 3 to 5 concrete criteria that a reviewer could verify FROM PHOTOS plus a short note.
  Each must be specific and objective (e.g. "Entire balcony wall is covered in white paint with no
  bare patches"), never vague ("good job").
- clarifications: list ambiguities (missing price, unclear scope). Empty list if none.

Client description:
\"\"\"{description}\"\"\""""

VERIFY_PROMPT = """You are ProofPay's proof-of-work verification agent. Money is held in escrow and
will be RELEASED TO THE WORKER automatically if you approve, so be fair but rigorous.

Deal:
- Worker: {worker}
- Task: {task}
- Amount: {amount} {currency}
- Deadline: {deadline}

Checklist (judge EACH item separately, in this order):
{checklist}

Worker's note: \"\"\"{note}\"\"\"
{n} photo(s) attached.

Instructions:
- For each checklist item return passed, confidence (0-1) and one-sentence reasoning grounded in what
  is visible. If a photo cannot show it, say so and lower confidence.
- overall = "approve" only if every item passes; "reject" if any item clearly fails; otherwise "uncertain".
- confidence = your overall confidence that the job described was actually completed.
- suspicious = true if the proof looks like a stock/internet photo (watermarks, studio lighting,
  perfect staging), a screenshot, an AI-generated image, unrelated to the task, or contradicts the note.
  List concrete suspicion_reasons.
- feedback_to_worker: if not approved, say exactly what to fix or what photo to take next.
  If approved, a short thank-you.
Extra signals from the server (treat as facts): {signals}"""


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
class GeminiAgent:
    def __init__(self, api_key: str, model: str = "gemini-3.8-flash", transport: httpx.BaseTransport | None = None):
        self.api_key = api_key
        self.model = model
        self.mode = f"gemini:{model}"
        self._http = httpx.Client(
            base_url="https://generativelanguage.googleapis.com", timeout=90.0, transport=transport
        )

    def _generate(self, parts: list[dict], schema: dict) -> dict:
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
                "responseSchema": schema,
            },
        }
        try:
            r = self._http.post(
                f"/v1beta/models/{self.model}:generateContent",
                json=body,
                headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
            )
        except httpx.HTTPError as e:
            raise AIError(f"Gemini request failed: {e}") from e
        if r.status_code != 200:
            try:
                msg = r.json().get("error", {}).get("message", r.text[:300])
            except ValueError:
                msg = r.text[:300]
            raise AIError(f"Gemini API {r.status_code}: {msg}")
        data = r.json()
        try:
            cand = data["candidates"][0]
            text = "".join(p.get("text", "") for p in cand["content"]["parts"] if not p.get("thought"))
        except (KeyError, IndexError):
            reason = (data.get("promptFeedback") or {}).get("blockReason") or "no candidates"
            raise AIError(f"Gemini returned no content ({reason})")
        return _loads_json(text)

    def parse_deal(self, description: str, today: str | None = None) -> dict:
        prompt = PARSE_PROMPT.format(description=description, today=today or date.today().isoformat())
        return normalize_deal(self._generate([{"text": prompt}], DEAL_SCHEMA), description)

    def verify_proof(self, deal: dict, note: str, photos: list[tuple[str, bytes]], signals: list[str] | None = None,
                     filenames: list[str] | None = None) -> dict:
        checklist = deal.get("checklist") or []
        prompt = VERIFY_PROMPT.format(
            worker=deal.get("worker_name"), task=deal.get("task"), amount=deal.get("amount"),
            currency=deal.get("currency"), deadline=deal.get("deadline"),
            checklist="\n".join(f"{i + 1}. {c}" for i, c in enumerate(checklist)),
            note=note or "(none)", n=len(photos), signals="; ".join(signals or []) or "none",
        )
        parts: list[dict] = [{"text": prompt}]
        for mime, blob in photos:
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(blob).decode()}})
        return normalize_verdict(self._generate(parts, VERDICT_SCHEMA), checklist)


# ---------------------------------------------------------------------------
# Deterministic mock
# ---------------------------------------------------------------------------
CHECKLIST_TEMPLATES = [
    (("paint", "painting", "repaint"), [
        "The {target} is fully covered in {color} paint with no bare or patchy areas",
        "Finish is even, with no visible streaks, drips or roller marks",
        "Edges, trim and surroundings are clean (no paint on floor, glass or fixtures)",
        "Photo clearly shows the whole {target} on site after completion",
    ]),
    (("tutor", "tutoring", "lesson", "teach", "class", "homework"), [
        "Photo shows the completed worksheet / notes from the session",
        "Note states the topics covered and the session length",
        "Evidence matches the agreed subject and student",
    ]),
    (("deliver", "delivery", "drop off", "pick up", "courier", "parcel", "package"), [
        "Photo shows the item at the agreed drop-off location",
        "Package appears intact and undamaged",
        "Note confirms the time of delivery and who received it",
    ]),
    (("clean", "cleaning", "wash", "tidy"), [
        "Photo shows the agreed area clean and free of debris",
        "Surfaces visibly wiped / washed with no stains left",
        "Trash and cleaning supplies have been removed",
    ]),
    (("logo", "design", "website", "code", "app", "edit", "video", "write", "article", "translate"), [
        "Screenshot or photo shows the finished deliverable",
        "Deliverable matches the requested scope",
        "Note links to or describes the files handed over",
    ]),
    (("fix", "repair", "install", "assemble", "mount", "plumb"), [
        "Photo shows the item installed / repaired in place",
        "Item appears functional and securely fitted",
        "Work area is cleaned up afterwards",
    ]),
]
STOP_NAMES = {"I", "My", "We", "Our", "The", "A", "An", "Please", "Need", "Pay", "Hire", "Get", "Mr", "Ms", "Mrs"}
REJECT_WORDS = ("not done", "incomplete", "half", "unfinished", "partial", "didn't finish", "did not finish",
                "ran out", "tomorrow", "fail")
SUSPICIOUS_WORDS = ("stock", "google", "internet", "downloaded", "screenshot", "shutterstock", "getty")


class MockAgent:
    """Deterministic rule-based agent so the demo works without any API key.

    Verification rules (documented in README):
      - no photo                    -> reject
      - note mentions unfinished work (e.g. "half done", "incomplete") -> reject
      - file name / note hints at a stock or downloaded image, or a
        server signal flags a reused photo -> uncertain + suspicious
      - otherwise                   -> approve (confidence ~0.9)
    """

    mode = "mock"

    def parse_deal(self, description: str, today: str | None = None) -> dict:
        text = " ".join(description.split())
        low = text.lower()
        amount = None
        m = re.search(r"(?:\$|usd\s?)(\d+(?:[.,]\d{1,2})?)", text, re.I) or re.search(
            r"(\d+(?:\.\d{1,2})?)\s?(?:usd|dollars|bucks|\$)", text, re.I)
        if m:
            amount = float(m.group(1).replace(",", "."))
        worker = "Worker"
        for cand in re.findall(r"\b([A-Z][a-z]{1,20})\b", text):
            if cand not in STOP_NAMES and cand.lower() not in {"monday", "tuesday", "wednesday", "thursday",
                                                                "friday", "saturday", "sunday", "today",
                                                                "tomorrow", "usd"}:
                worker = cand
                break
        dl = re.search(r"\bby\s+((?:next\s+)?[A-Za-z0-9 ,/-]+?)(?:[.,;!]|$| for | and )", text, re.I)
        deadline = dl.group(1).strip() if dl else "Not specified"
        task = text
        tm = re.search(rf"{re.escape(worker)}\s+(?:will|is going to|to|should|must|can)\s+(.+?)(?:\s+for\s+\$|\s+for\s+\d|\s+by\s+|[.]|$)", text)
        if tm:
            task = tm.group(1).strip()
        task = task[0].upper() + task[1:] if task else "Complete the agreed work"
        checklist = None
        for keys, tpl in CHECKLIST_TEMPLATES:
            if any(k in low for k in keys):
                color = next((c for c in ("white", "black", "blue", "green", "red", "yellow", "grey", "gray",
                                          "beige", "cream") if c in low), "the agreed")
                target = next((t for t in ("balcony wall", "wall", "fence", "door", "room", "ceiling", "gate")
                               if t in low), "surface")
                checklist = [c.format(color=color, target=target) for c in tpl]
                break
        if not checklist:
            checklist = [
                f"Photo clearly shows the completed task: {task}",
                "Work matches the agreed description and scope",
                "Photo is taken on site (not a screenshot or downloaded image)",
                "Note explains what was done and when",
            ]
        clar = [] if amount else ["No price found - please set the amount."]
        if deadline == "Not specified":
            clar.append("No deadline found - consider adding one.")
        return normalize_deal({
            "worker_name": worker, "task": task, "amount": amount or 0, "currency": "USD",
            "deadline": deadline, "checklist": checklist, "clarifications": clar,
        }, description)

    def verify_proof(self, deal: dict, note: str, photos: list[tuple[str, bytes]], signals: list[str] | None = None,
                     filenames: list[str] | None = None) -> dict:
        checklist = deal.get("checklist") or []
        note_l = (note or "").lower()
        names_l = " ".join(filenames or []).lower()
        seed = hashlib.sha256((note or "").encode() + b"".join(b for _, b in photos)).digest()

        def conf(i: int, base: float) -> float:
            return round(min(0.99, base + (seed[i % len(seed)] % 8) / 100), 2)

        reasons = [s for s in (signals or [])]
        if any(w in note_l or w in names_l for w in SUSPICIOUS_WORDS):
            reasons.append("Image appears to be a stock / downloaded photo rather than an on-site picture")
        suspicious = bool(reasons)

        if not photos:
            items = [{"criterion": c, "passed": False, "confidence": 0.95,
                      "reasoning": "No photo was provided, so this cannot be verified."} for c in checklist]
            return normalize_verdict({
                "items": items, "overall": "reject", "confidence": 0.05, "suspicious": False,
                "summary": "No photographic proof submitted.",
                "feedback_to_worker": "Please upload at least one clear photo of the finished work.",
            }, checklist)

        if any(w in note_l for w in REJECT_WORDS):
            items = []
            for i, c in enumerate(checklist):
                failed = i == 0 or ("edge" in c.lower() or "even" in c.lower())
                items.append({
                    "criterion": c, "passed": not failed, "confidence": conf(i, 0.8),
                    "reasoning": ("The photo and note indicate this part is not finished yet."
                                  if failed else "This part looks consistent with the photo."),
                })
            failed_names = [it["criterion"] for it in items if not it["passed"]]
            return normalize_verdict({
                "items": items, "overall": "reject", "confidence": 0.22, "suspicious": suspicious,
                "suspicion_reasons": reasons,
                "summary": f"{len(failed_names)} of {len(items)} checklist items are not met yet.",
                "feedback_to_worker": "Not quite there yet. Please finish and re-photograph: "
                                      + "; ".join(failed_names) + ".",
            }, checklist)

        if suspicious:
            items = [{"criterion": c, "passed": True, "confidence": conf(i, 0.45),
                      "reasoning": "Looks done, but the image may not be an original on-site photo."}
                     for i, c in enumerate(checklist)]
            return normalize_verdict({
                "items": items, "overall": "uncertain", "confidence": 0.48, "suspicious": True,
                "suspicion_reasons": reasons,
                "summary": "The work appears complete but the proof itself looks unreliable.",
                "feedback_to_worker": "Please take a fresh photo on site with your phone camera, "
                                      "ideally including something that shows the location.",
            }, checklist)

        items = [{"criterion": c, "passed": True, "confidence": conf(i, 0.86),
                  "reasoning": "Clearly visible in the submitted photo and consistent with the note."}
                 for i, c in enumerate(checklist)]
        return normalize_verdict({
            "items": items, "overall": "approve", "confidence": conf(31, 0.88), "suspicious": False,
            "summary": "All checklist items are satisfied by the photo evidence.",
            "feedback_to_worker": f"Great work, {deal.get('worker_name') or 'thanks'}! Payment is being released.",
        }, checklist)


# ---------------------------------------------------------------------------
# Normalisation (never trust model output blindly)
# ---------------------------------------------------------------------------
def _loads_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        data = json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise AIError("Model did not return JSON")
        data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise AIError("Model returned non-object JSON")
    return data


def _clamp(x, lo=0.0, hi=1.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if v > 1.0 and hi == 1.0 and v <= 100:  # model answered in percent
        v = v / 100
    return max(lo, min(hi, v))


def money(x) -> str:
    try:
        v = float(str(x).replace("$", "").replace(",", "").strip() or 0)
    except ValueError:
        v = 0.0
    return f"{max(0.0, v):.2f}"


def normalize_deal(d: dict, description: str = "") -> dict:
    checklist = [str(c).strip() for c in (d.get("checklist") or []) if str(c).strip()][:6]
    if not checklist:
        checklist = ["Photo clearly shows the completed work"]
    currency = str(d.get("currency") or "USD").upper()[:3]
    clar = [str(c) for c in (d.get("clarifications") or []) if str(c).strip()]
    if currency != "USD":
        clar.append(f"Amount was stated in {currency}; ProofPay settles in USD - please confirm the USD price.")
        currency = "USD"
    return {
        "worker_name": (str(d.get("worker_name") or "Worker").strip())[:60],
        "task": (str(d.get("task") or description).strip())[:300],
        "amount": money(d.get("amount")),
        "currency": currency,
        "deadline": (str(d.get("deadline") or "Not specified").strip())[:80],
        "checklist": checklist,
        "clarifications": clar,
    }


def normalize_verdict(v: dict, checklist: list[str]) -> dict:
    raw_items = v.get("items") or []
    items = []
    for i, crit in enumerate(checklist):
        it = raw_items[i] if i < len(raw_items) and isinstance(raw_items[i], dict) else {}
        items.append({
            "criterion": crit,
            "passed": bool(it.get("passed", False)),
            "confidence": round(_clamp(it.get("confidence", 0)), 2),
            "reasoning": str(it.get("reasoning") or "Not assessed by the model.")[:500],
        })
    overall = str(v.get("overall") or "uncertain").lower()
    if overall not in {"approve", "reject", "uncertain"}:
        overall = "uncertain"
    if overall == "approve" and not all(it["passed"] for it in items):
        overall = "uncertain"  # model contradicted itself; be safe
    return {
        "items": items,
        "overall": overall,
        "confidence": round(_clamp(v.get("confidence", 0)), 2),
        "summary": str(v.get("summary") or "")[:600],
        "feedback_to_worker": str(v.get("feedback_to_worker") or "")[:800],
        "suspicious": bool(v.get("suspicious", False)),
        "suspicion_reasons": [str(r) for r in (v.get("suspicion_reasons") or [])][:6],
    }
