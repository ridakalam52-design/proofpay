/* ProofPay front-end — vanilla JS, no build step. */
(function () {
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const pct = (x) => Math.round((Number(x) || 0) * 100) + '%';
  const fmtTime = (iso) => { try { return new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }); } catch { return iso; } };
  const TERMINAL = ['paid', 'voided'];
  const ACTOR_ICON = { client: '🧑', worker: '🛠️', ai: '🤖', paypal: '💳', system: '⚙️' };

  window.toast = function (msg, isErr) {
    const t = document.getElementById('toast');
    t.textContent = msg; t.className = 'toast show' + (isErr ? ' err' : '');
    clearTimeout(t._h); t._h = setTimeout(() => (t.className = 'toast'), 4200);
  };
  window.busy = function (btn, on, label) {
    if (!btn) return;
    if (on) { btn._label = btn.innerHTML; btn.disabled = true; btn.innerHTML = '<span class="spin"></span> ' + esc(label || 'Working…'); }
    else { btn.disabled = false; if (btn._label) btn.innerHTML = btn._label; }
  };
  window.api = async function (url, opts = {}) {
    const init = { method: opts.method || 'GET', headers: {} };
    if (opts.form) init.body = opts.form;
    else if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers['Content-Type'] = 'application/json'; }
    const r = await fetch(url, init);
    let data = null; try { data = await r.json(); } catch { /* empty */ }
    if (!r.ok) throw new Error((data && (data.error || data.detail)) || ('Request failed (' + r.status + ')'));
    return data;
  };

  const statusPill = (d) => `<span class="pill pill-${esc(d.status)}">${esc(d.status_label)}</span>`;

  function dealHeader(d, role) {
    const who = role === 'client' ? `You pay <b>${esc(d.worker_name)}</b>` : `You'll be paid by the client`;
    return `
      <section class="card deal-head">
        <div class="row between wrap-gap">
          <span class="muted mono">${esc(d.id)}</span>${statusPill(d)}
        </div>
        <div class="amount">$${esc(d.amount)} <small>${esc(d.currency)}</small></div>
        <p class="who">${who}</p>
        <h2 class="task">${esc(d.task)}</h2>
        <p class="muted">⏰ Due: ${esc(d.deadline)}</p>
        ${d.status === 'paid' ? `<div class="banner ok">✅ Work proven — $${esc(d.amount)} captured via PayPal${d.capture_id ? ` <span class="mono">(${esc(d.capture_id)})</span>` : ''}.</div>` : ''}
        ${d.status === 'voided' ? `<div class="banner muted-b">↩️ Deal cancelled — the PayPal hold was voided and no money moved.</div>` : ''}
        ${d.status === 'needs_review' && role === 'client' ? `<div class="banner warn">👀 The AI wasn't sure enough to move money. Please review the proof below and decide.</div>` : ''}
      </section>`;
  }

  function checklistCard(d, title) {
    return `<section class="card"><h3>${esc(title)}</h3><ol class="checklist">${d.checklist.map((c) => `<li>${esc(c)}</li>`).join('')}</ol></section>`;
  }

  function verdictBlock(sub, idx, total) {
    const v = sub.verdict;
    const photos = sub.photos.map((p) => `<a href="${esc(p)}" target="_blank" rel="noopener"><img src="${esc(p)}" alt="proof photo" loading="lazy"></a>`).join('');
    if (!v) return `<div class="sub"><div class="photos">${photos}</div><p class="muted">Checking…</p></div>`;
    const decisionLabel = { auto_release: '✅ Approved — payment released', changes_requested: '✏️ Changes requested', needs_review: '👀 Sent to client review' }[sub.decision] || sub.decision;
    return `
      <div class="sub">
        <div class="row between wrap-gap">
          <b>Submission ${idx + 1}${idx === total - 1 ? ' (latest)' : ''}</b>
          <span class="muted small">${fmtTime(sub.created_at)} · ${esc(v.agent || '')}</span>
        </div>
        <div class="photos">${photos}</div>
        ${sub.note ? `<p class="note">“${esc(sub.note)}”</p>` : ''}
        <div class="verdict v-${esc(v.overall)}">
          <div class="row between wrap-gap">
            <b>AI verdict: ${esc(v.overall.toUpperCase())}</b>
            <span>Confidence <b>${pct(v.confidence)}</b> <span class="muted small">(auto-release ≥ ${pct(v.threshold ?? 0.75)})</span></span>
          </div>
          <div class="bar"><i style="width:${pct(v.confidence)}"></i></div>
          <p>${esc(v.summary)}</p>
          <ul class="items">
            ${v.items.map((it) => `
              <li class="${it.passed ? 'pass' : 'fail'}">
                <span class="mark">${it.passed ? '✓' : '✗'}</span>
                <div><div>${esc(it.criterion)} <span class="muted small">· ${pct(it.confidence)}</span></div>
                <div class="muted small">${esc(it.reasoning)}</div></div>
              </li>`).join('')}
          </ul>
          ${v.suspicious ? `<div class="banner warn small">🚩 Suspicious proof: ${esc((v.suspicion_reasons || []).join('; ') || 'flagged by AI')}</div>` : ''}
          ${v.feedback_to_worker ? `<p class="feedback"><b>Feedback to worker:</b> ${esc(v.feedback_to_worker)}</p>` : ''}
          <p class="small"><b>${esc(decisionLabel)}</b></p>
        </div>
      </div>`;
  }

  function submissionsCard(d) {
    if (!d.submissions.length) return '';
    const subs = d.submissions.slice().map((s, i) => verdictBlock(s, i, d.submissions.length)).reverse().join('');
    return `<section class="card"><h3>Proof & AI verification</h3>${subs}</section>`;
  }

  function timelineCard(d) {
    return `<section class="card"><h3>Deal timeline</h3><ul class="timeline">
      ${d.events.slice().reverse().map((e) => `
        <li class="ev ev-${esc(e.kind)}"><span class="ico">${ACTOR_ICON[e.actor] || '•'}</span>
          <div><div>${esc(e.message)}</div><div class="muted small">${fmtTime(e.ts)} · ${esc(e.actor)}</div></div></li>`).join('')}
    </ul></section>`;
  }

  // ------------------------------------------------------------------ client
  function renderClient(root, d, token, paypalMock) {
    const clar = JSON.parse(sessionStorage.getItem('pp-clar-' + d.id) || '[]');
    let html = dealHeader(d, 'client');
    if (d.status === 'draft') {
      html += `
      ${clar.length ? `<section class="card banner warn"><b>AI wants you to double-check:</b><ul>${clar.map((c) => `<li>${esc(c)}</li>`).join('')}</ul></section>` : ''}
      <section class="card">
        <h3>1 · Review the terms</h3>
        <p class="muted small">Extracted by AI from: “${esc(d.description)}”. Edit anything before funding — terms lock once the money is held.</p>
        <form id="edit-form" class="grid">
          <label>Worker<input name="worker_name" value="${esc(d.worker_name)}" required></label>
          <label>Amount (USD)<input name="amount" type="number" min="1" step="0.01" value="${esc(d.amount)}" required></label>
          <label class="full">Task<input name="task" value="${esc(d.task)}" required></label>
          <label class="full">Deadline<input name="deadline" value="${esc(d.deadline)}" required></label>
          <label class="full">Proof checklist <span class="muted small">(one per line — the AI judges each)</span>
            <textarea name="checklist" rows="5">${esc(d.checklist.join('\n'))}</textarea></label>
          <div class="full"><button class="btn" type="submit" id="save-btn">Save terms</button></div>
        </form>
      </section>
      <section class="card">
        <h3>2 · Fund the escrow</h3>
        <p class="muted">PayPal <b>authorizes</b> $${esc(d.amount)} on your account — it is held, not charged, until the work is proven. You can cancel any time before release.</p>
        ${paypalMock
          ? `<button class="btn btn-paypal btn-lg" id="mock-pay"><b>Pay</b><i>Pal</i>&nbsp; Authorize $${esc(d.amount)}</button>
             <p class="muted small">Mock PayPal mode (no keys set): simulates create order → buyer approval → authorize.</p>`
          : `<div id="paypal-buttons"></div><p class="muted small">Sandbox: log in with your PayPal <b>sandbox personal</b> test account.</p>`}
      </section>`;
    } else {
      html += `
      <section class="card share">
        <h3>Share with ${esc(d.worker_name)}</h3>
        <p class="muted small">Send this link — it lets them see the checklist and upload proof. It can't move money.</p>
        <div class="row"><input id="share" readonly value="${esc(location.origin + d.worker_url)}"><button class="btn" id="copy">Copy</button></div>
        <a class="small" href="${esc(d.worker_url)}" target="_blank" rel="noopener">Open worker view ↗</a>
      </section>`;
      if (d.can.release || d.can.void) {
        html += `
        <section class="card actions">
          <h3>Your controls</h3>
          <div class="row wrap-gap">
            ${d.can.release ? `<button class="btn btn-primary" id="release">Release $${esc(d.amount)} now</button>` : ''}
            ${d.can.request_changes ? `<button class="btn" id="changes">Ask for changes</button>` : ''}
            ${d.can.void ? `<button class="btn btn-danger" id="void">Cancel & void hold</button>` : ''}
          </div>
          <p class="muted small">The AI releases automatically when every checklist item passes with high confidence. You can always override.</p>
        </section>`;
      }
      html += submissionsCard(d);
      html += checklistCard(d, 'Proof checklist');
    }
    html += timelineCard(d);
    root.innerHTML = html;

    // wire up
    const ef = document.getElementById('edit-form');
    if (ef) ef.addEventListener('submit', async (e) => {
      e.preventDefault();
      const f = new FormData(ef); const btn = document.getElementById('save-btn');
      busy(btn, true, 'Saving…');
      try {
        const nd = await api(`/api/c/${token}`, { method: 'PATCH', body: {
          worker_name: f.get('worker_name'), task: f.get('task'), amount: f.get('amount'), deadline: f.get('deadline'),
          checklist: String(f.get('checklist')).split('\n').map((s) => s.trim()).filter(Boolean) } });
        toast('Terms saved'); state.set(nd);
      } catch (err) { toast(err.message, true); busy(btn, false); }
    });
    const mock = document.getElementById('mock-pay');
    if (mock) mock.addEventListener('click', async () => {
      busy(mock, true, 'Authorizing with PayPal…');
      try {
        await saveIfDirty(token);
        const order = await api(`/api/c/${token}/paypal/order`, { method: 'POST' });
        const nd = await api(`/api/c/${token}/paypal/authorize`, { method: 'POST', body: { orderID: order.id } });
        toast('Funds authorized and held 🔒'); sessionStorage.removeItem('pp-clar-' + d.id); state.set(nd);
      } catch (err) { toast(err.message, true); busy(mock, false); }
    });
    const ppDiv = document.getElementById('paypal-buttons');
    if (ppDiv) {
      if (!window.paypal) { ppDiv.innerHTML = '<p class="banner warn">PayPal SDK failed to load — check PAYPAL_CLIENT_ID.</p>'; }
      else window.paypal.Buttons({
        style: { layout: 'vertical', shape: 'pill', label: 'pay' },
        createOrder: async () => { await saveIfDirty(token); return (await api(`/api/c/${token}/paypal/order`, { method: 'POST' })).id; },
        onApprove: async (data) => {
          try {
            const nd = await api(`/api/c/${token}/paypal/authorize`, { method: 'POST', body: { orderID: data.orderID } });
            toast('Funds authorized and held 🔒'); sessionStorage.removeItem('pp-clar-' + d.id); state.set(nd);
          } catch (err) { toast(err.message, true); }
        },
        onCancel: () => toast('PayPal checkout cancelled'),
        onError: (err) => toast('PayPal error: ' + (err && err.message ? err.message : err), true),
      }).render('#paypal-buttons');
    }
    const copy = document.getElementById('copy');
    if (copy) copy.addEventListener('click', async () => {
      const inp = document.getElementById('share'); inp.select();
      try { await navigator.clipboard.writeText(inp.value); } catch { document.execCommand('copy'); }
      toast('Link copied');
    });
    const act = (id, url, label, confirmMsg, body) => {
      const b = document.getElementById(id); if (!b) return;
      b.addEventListener('click', async () => {
        if (confirmMsg && !confirm(confirmMsg)) return;
        let payload = body;
        if (id === 'changes') { const n = prompt('What should the worker change?'); if (n === null) return; payload = { note: n }; }
        busy(b, true, label);
        try { const nd = await api(url, { method: 'POST', body: payload }); state.set(nd); toast('Done'); }
        catch (err) { toast(err.message, true); busy(b, false); }
      });
    };
    act('release', `/api/c/${token}/release`, 'Capturing…', `Release $${d.amount} to ${d.worker_name} now?`);
    act('void', `/api/c/${token}/void`, 'Voiding…', 'Cancel the deal and release the hold on your PayPal funds?');
    act('changes', `/api/c/${token}/request-changes`, 'Sending…');
  }

  async function saveIfDirty(token) {
    const ef = document.getElementById('edit-form'); if (!ef) return;
    const f = new FormData(ef);
    await api(`/api/c/${token}`, { method: 'PATCH', body: {
      worker_name: f.get('worker_name'), task: f.get('task'), amount: f.get('amount'), deadline: f.get('deadline'),
      checklist: String(f.get('checklist')).split('\n').map((s) => s.trim()).filter(Boolean) } });
  }

  // ------------------------------------------------------------------ worker
  function renderWorker(root, d, token) {
    let html = dealHeader(d, 'worker');
    const last = d.submissions[d.submissions.length - 1];
    if (d.status === 'draft') html += `<section class="card banner muted-b">⏳ The client hasn't funded this deal yet. You'll be able to submit proof once the money is held by PayPal.</section>`;
    if (d.status === 'funded') html += `<section class="card banner ok">🔒 $${esc(d.amount)} is held by PayPal for you. Finish the job, then upload proof below.</section>`;
    if (d.status === 'changes_requested') {
      const fb = last && last.verdict ? last.verdict.feedback_to_worker : '';
      const fails = last && last.verdict ? last.verdict.items.filter((i) => !i.passed) : [];
      const clientNote = d.events.slice().reverse().find((e) => e.kind === 'changes_requested' && e.actor === 'client');
      html += `<section class="card banner warn"><b>Almost there — please fix and resubmit.</b>
        ${fb ? `<p>${esc(fb)}</p>` : ''}${clientNote ? `<p>${esc(clientNote.message)}</p>` : ''}
        ${fails.length ? `<ul>${fails.map((i) => `<li>✗ ${esc(i.criterion)} — <span class="small">${esc(i.reasoning)}</span></li>`).join('')}</ul>` : ''}</section>`;
    }
    if (d.status === 'needs_review') html += `<section class="card banner muted-b">👀 Your proof is with the client for a final look. You'll get paid as soon as they release it.</section>`;
    if (d.status === 'paid') html += `<section class="card banner ok big">🎉 You've been paid $${esc(d.amount)}. Thanks for the great work!</section>`;
    html += checklistCard(d, 'What the AI will check');
    if (d.can.submit) {
      html += `
      <section class="card">
        <h3>Submit proof</h3>
        <form id="proof-form">
          <label class="drop" for="photos">
            <input id="photos" name="photos" type="file" accept="image/*" capture="environment" multiple required>
            <span>📷 Take or choose up to 4 photos of the finished work</span>
          </label>
          <div id="previews" class="photos"></div>
          <label>Note to the client<textarea name="note" rows="3" placeholder="e.g. Two coats done, edges taped, floor cleaned."></textarea></label>
          <button class="btn btn-primary btn-lg" id="proof-btn" type="submit">Send proof for AI check</button>
          <p class="muted small">Tip: photograph each checklist item clearly, in good light, on site.</p>
        </form>
      </section>`;
    }
    html += submissionsCard(d);
    html += timelineCard(d);
    root.innerHTML = html;

    const pf = document.getElementById('proof-form');
    if (pf) {
      const input = document.getElementById('photos');
      input.addEventListener('change', () => {
        const pv = document.getElementById('previews'); pv.innerHTML = '';
        [...input.files].slice(0, 4).forEach((f) => { const img = document.createElement('img'); img.src = URL.createObjectURL(f); pv.appendChild(img); });
      });
      pf.addEventListener('submit', async (e) => {
        e.preventDefault();
        const fd = new FormData(); fd.append('note', pf.note.value);
        [...input.files].slice(0, 4).forEach((f) => fd.append('photos', f, f.name));
        const btn = document.getElementById('proof-btn');
        busy(btn, true, 'AI is checking your proof…');
        state.pause = true;
        try { const nd = await api(`/api/w/${token}/proof`, { method: 'POST', form: fd }); state.pause = false; state.set(nd);
          toast(nd.status === 'paid' ? 'Approved — payment released! 🎉' : nd.status === 'changes_requested' ? 'Some items need fixing — see feedback' : 'Sent to the client for review');
        } catch (err) { state.pause = false; toast(err.message, true); busy(btn, false); }
      });
    }
  }

  // ------------------------------------------------------------------- boot
  const state = { data: null, pause: false, set: null };
  window.ProofPay = {
    boot() {
      const root = document.getElementById('app');
      const role = root.dataset.role, token = root.dataset.token, paypalMock = root.dataset.paypalMock === 'true';
      const url = role === 'client' ? `/api/c/${token}` : `/api/w/${token}`;
      state.set = (d) => {
        state.data = d; state._json = JSON.stringify(d);
        role === 'client' ? renderClient(root, d, token, paypalMock) : renderWorker(root, d, token);
      };
      api(url).then(state.set).catch((e) => { root.innerHTML = `<section class="card banner warn">${esc(e.message)}</section>`; });
      // live updates (client sees the AI verdict land; worker sees funding / review decisions)
      setInterval(async () => {
        if (state.pause || !state.data || TERMINAL.includes(state.data.status) || document.hidden) return;
        if (state.data.status === 'draft' && role === 'client') return; // don't clobber the edit form
        const ae = document.activeElement;
        if (ae && (ae.tagName === 'TEXTAREA' || (ae.tagName === 'INPUT' && ae.type !== 'button'))) return;
        if (document.getElementById('photos') && document.getElementById('photos').files.length) return;
        try { const d = await api(url); if (JSON.stringify(d) !== state._json) state.set(d); } catch { /* offline */ }
      }, 4000);
    },
  };
})();
