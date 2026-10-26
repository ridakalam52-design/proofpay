# ProofPay: 2.5-minute demo video script

**Target length:** 2:30 (the hard limit is 3:00).
**Setup before recording:**
- Run in **real sandbox mode** (Gemini + PayPal keys in `.env`) so judges see real Smart Buttons and real AI reasoning.
- Clear old data with `rm -rf data/`, then start `uvicorn app.main:app`.
- Open two browser windows side by side: **client** (laptop) on the left, **worker** (phone-sized window, or a real phone) on the right.
- Log in at sandbox.paypal.com in a third tab ahead of time, ready to show the authorization afterwards.
- Have both photos ready: `demo_assets/wall_half.jpg` and `demo_assets/wall_done.jpg`, or real photos of a real job.
- **Live-mode note:** the sample photos in `demo_assets/` are AI-generated, and real Gemini may (correctly!) flag them as not genuine. For the real-mode take, photograph an actual small job, such as a wall touch-up, a tidied desk or a delivered parcel, and edit the deal text to match.
- **Tip:** a photo reused across deals is flagged as suspicious, which is on purpose. Use fresh data or new photos for each take.

---

### 0:00–0:15 · Hook
**Screen:** title card, then the ProofPay home page.
**Voice-over:**
> "Every day, millions of small jobs are agreed over chat: a painter, a tutor, a delivery run. Someone always has to trust first. Either the client pays and hopes, or the worker works and chases. ProofPay fixes that: **pay when the work is proven.**"

### 0:15–0:40 · AI turns a chat message into a contract
**Screen:** click the 🎨 Painter chip (`Ravi will paint my balcony wall white for $40 by Saturday`), then **Create deal with AI**.
**Voice-over:**
> "I describe the deal the way I'd text it. Gemini pulls out the worker, the task, $40, the deadline, and most importantly a **checklist of proof a photo can actually show**: fully white, even finish, clean edges. I can tweak anything before I fund it."

*(Briefly edit one checklist line to show it's editable.)*

### 0:40–1:05 · PayPal holds the money
**Screen:** click the PayPal button → sandbox pop-up → log in as the sandbox buyer → approve. The status changes to **Funded – waiting for proof**. Point to the timeline line *"$40 authorized and held – not captured"*.
**Voice-over:**
> "I pay with PayPal, but ProofPay creates the order with **intent AUTHORIZE**. The $40 is held on my account, not charged. Ravi can see the money is real, and I keep control. Now I just share this link with him."

*(Click **Copy**, then show the worker window.)*

### 1:05–1:40 · Worker submits proof and the AI rejects it with specific feedback
**Screen (worker):** the green "$40 is held by PayPal for you" banner and the checklist. Upload `wall_half.jpg`, note "Done!", then **Send proof for AI check**.
**Voice-over:**
> "Ravi sees exactly what the AI will check. Say he tries to get paid early with a half-painted wall…"

**Screen:** the verdict appears: **REJECT**, with ✗ on "fully covered", reasoning about the unpainted right half, and the feedback box.
> "The Gemini vision agent checks **every item separately**, with a confidence score and its reasoning, and tells him exactly what to fix. No money moved."

### 1:40–2:10 · Resubmit, approve, capture
**Screen (worker):** upload `wall_done.jpg`, note "Second coat done, floor cleaned". The verdict is **APPROVE**, all ✓, confidence around 90%, then the **"You've been paid $40"** banner.
**Screen (client):** the page updates live to **Paid – work proven**, showing the capture ID. Cut to the sandbox.paypal.com activity showing the completed payment.
**Voice-over:**
> "He finishes the job and sends a new photo. Every criterion passes above our 75% threshold, so the server **captures the PayPal authorization**. Ravi is paid in seconds, and my timeline shows the whole audit trail."

### 2:10–2:25 · Safety rails (quick montage)
**Screen:** a second deal showing the 🚩 *"Photo was already used as proof for a different deal"* flag → **Needs client review** → the client buttons **Release / Ask for changes / Cancel & void hold**.
**Voice-over:**
> "If the AI isn't sure, or the proof looks reused or like a stock photo, it never moves money on its own. I decide, and I can **void** the hold any time before release."

### 2:25–2:30 · Close
**Screen:** home page with the tagline, and the repo URL on screen.
**Voice-over:**
> "ProofPay: PayPal authorizations plus an AI that checks the work. Pay when the work is proven."

---

**Recording tips**
- Record at 1080p and zoom the browser to 110–125% so judges can read the verdict.
- If Gemini takes a few seconds, cut the wait in editing. The spinner says "AI is checking your proof…".
- Keep mock mode as your backup take. The flow is identical, and the badges show `mock`.
