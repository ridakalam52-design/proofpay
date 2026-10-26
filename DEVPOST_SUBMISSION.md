# Devpost submission: ProofPay

## Title
**ProofPay: pay when the work is proven**

## Tagline
Escrow for everyday gig deals: PayPal holds the money, and an AI agent checks photo proof of the work before releasing it.

## Inspiration
Most of the world's small jobs never touch a marketplace. A neighbour paints a wall, a college student tutors maths, a cousin does a delivery run. The deal is a WhatsApp message, and payment rests on trust. Someone always has to go first: the client pays and hopes, or the worker finishes and chases. Formal escrow platforms are too heavy and too expensive for a $40 job. We wanted the trust of escrow with the effort of sending a text.

## What it does
1. **Describe the deal in plain language.** For example: *"Ravi will paint my balcony wall white for $40 by Saturday."* A Gemini agent turns it into structured terms (worker, task, amount, deadline) and a **checklist of proof criteria a photo can verify**. The client can edit everything before funding.
2. **Fund with PayPal, held and not charged.** The client pays with PayPal Smart Buttons. The order uses **Orders v2 `intent=AUTHORIZE`**, so the funds are authorized and held, not captured.
3. **The worker proves the job.** A share link shows the worker the checklist and lets them upload photos and a note from their phone.
4. **AI vision verification.** Gemini judges the proof **item by item**, giving pass/fail, a confidence score and reasoning for each, an overall verdict, and feedback for the worker. It flags suspicious proof such as stock-photo style images, screenshots, a photo that doesn't match the note, or (checked on the server by hash) a photo reused from another deal.
5. **Money moves only on proof.**
   - Approved with high confidence → the server **captures** the authorization (`/v2/payments/authorizations/{id}/capture`).
   - Rejected → the worker gets specific fixes and resubmits.
   - Unsure or suspicious → the client reviews and can release, ask for changes, or **void** the hold (`/void`).
   - Every step lands on a live deal timeline / audit log.

## How we built it
- **Backend:** Python FastAPI with a small, explicit escrow state machine (`draft → funded → verifying → paid | changes_requested | needs_review | voided`) on SQLite. Atomic compare-and-set status updates stop double-captures when requests race.
- **PayPal:** OAuth client-credentials, **Orders v2** (create with `AUTHORIZE`, then authorize), and **Payments v2** (capture with `final_capture`, and void). Every money-moving call sends a `PayPal-Request-Id` for idempotency. The front end uses the **PayPal JS SDK** with `intent=authorize`.
- **AI:** The Google Gemini REST API (`gemini-3.1-flash-lite` with an automatic fallback chain, configurable) with **`responseSchema` structured JSON output** for both agents, and images sent inline for vision. We never trust model output blindly. A normalisation layer clamps confidences, lines verdicts up with the checklist, and downgrades any "approve" that has a failing item.
- **Async AI:** deal creation and proof upload respond instantly; Gemini refines and reviews in the background and the page updates live. A single-capture guard means a verdict can only ever release money once.
- **Frontend:** Jinja and vanilla JS, mobile-first, no build step. Pages poll for updates, so the client sees the verdict land live.
- **Judge-friendly:** a **mock mode** turns on automatically when keys are missing (simulated PayPal authorize/capture/void and a deterministic AI stub), so anyone can run the full flow in under a minute. There are pytest tests for the whole parse → authorize → verify → capture/void flow, and the real PayPal and Gemini clients are tested against fake HTTP transports. The repo includes a Render blueprint for a free hosted demo.

## Challenges we ran into
- **Deciding when an AI may move money.** A verdict and a payment are different things. We split them: the AI produces evidence (per-item confidence and reasoning), and a separate, auditable policy decides whether to capture, ask for changes, or escalate to a human. AI errors fail safe and send the deal to client review.
- **Writing checklists that can be checked.** "Do a good job" can't be verified. Prompting Gemini to produce criteria that are *objective and photo-checkable* made verification far more reliable.
- **Authorization lifecycle.** Handling authorize vs. capture vs. void correctly, plus idempotent retries and race conditions, so a double-click can never pay twice.
- **Cheap fraud resistance.** Hashing photos to catch proof reused across deals, and feeding that fact to the model as context.

## Accomplishments that we're proud of
- A full escrow loop on real PayPal sandbox rails, with AI in the money path and humans in control.
- Feedback a worker can act on ("the right half of the wall is still unpainted") instead of a bare "rejected".
- One command to run, with no keys needed to try it.

## What we learned
- PayPal's authorize-then-capture model is a natural fit for "trust but verify" commerce.
- Structured output (`responseSchema`) turns an LLM from a chatbot into a component you can rely on.
- Confidence thresholds plus human override are a practical way to put AI in charge of real money.

## What's next for ProofPay
- **Milestones:** partial captures per checklist item for bigger jobs.
- **Direct payouts** to workers via PayPal Payouts, plus worker reputation built on verified jobs.
- Stronger proof: live-camera capture, EXIF time and location checks, before/after pairs, short video proof.
- WhatsApp and SMS deal links, deadline reminders, and automatic void when a deadline passes.
- Dispute mediation where the AI summarises the evidence for both sides.

## Built with
`python` · `fastapi` · `sqlite` · `jinja2` · `javascript` · `paypal-orders-v2` · `paypal-js-sdk` · `google-gemini` · `gemini-3.1-flash-lite` · `render`

## Links
- **Repo:** https://github.com/ridakalam52-design/proofpay *(public, MIT)*
- **Live demo:** https://proofpay-a167.onrender.com *(free Render instance: first load may take ~60s to wake; PayPal sandbox only)*
- **Video:** https://youtu.be/l7d-wotfKDM
