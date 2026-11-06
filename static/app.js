/* Udyam Digital — the journey, plus a live log of what the backend actually did.
   Vanilla JS. No framework, no CDN, no build step. */

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g,
  c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;' }[c]));
const inr = n => '₹' + Number(n || 0).toLocaleString('en-IN');
const make = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
};
const post = (url, body) => fetch(url, {
  method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body || {})
}).then(r => r.json());
const get = url => fetch(url).then(r => r.json());
const wait = ms => new Promise(r => setTimeout(r, ms));

const S = {
  sid:null, busy:false, seen:new Set(),
  profile:null, bundle:null, chosen:[], addons:[],
  grants:{ identity_verification:false, service_delivery:false,
           service_updates:true, marketing:false },
  applicant:'K03', order:null,
};

/* ─────────────── system activity log ─────────────── */
function logHead(text){
  const li = make('li', 'head', esc(text));
  $('log').append(li);
  $('log').scrollTop = $('log').scrollHeight;
}
function log(html, kind){
  const li = make('li', kind || '', html);
  $('log').append(li);
  $('log').scrollTop = $('log').scrollHeight;
  return li;
}
function railFoot(html){ $('railfoot').innerHTML = html; }

/* ─────────────── screen routing ─────────────── */
const SCREENS = ['chat','quote','consent','kyc','tracker'];
function show(name){
  SCREENS.forEach(s => $('view-' + s).hidden = (s !== name));
  $('composer').hidden = (name !== 'chat');
  $('chips').hidden = (name !== 'chat');
  $('back').hidden = (name === 'chat' || name === 'tracker');
  const at = SCREENS.indexOf(name);
  [...$('steps').children].forEach((el, i) => {
    el.className = i < at ? 'done' : i === at ? 'now' : '';
  });
  const pane = $('view-' + name);
  if (pane) pane.scrollTop = 0;
}

/* ═══════════════ 1. needs ═══════════════ */
function say(text, who, kind){
  const row = make('div', 'turn' + (who === 'self' ? ' self' : ''));
  row.append(make('div', 'said ' + (who === 'self' ? 'self' : kind || 'them'), esc(text)));
  $('chat').append(row);
  $('chat').scrollTop = $('chat').scrollHeight;
}
function thinking(on){
  $('dots')?.remove();
  if (!on) return;
  const row = make('div','turn'); row.id = 'dots';
  row.append(make('div','said them dots','<i></i><i></i><i></i>'));
  $('chat').append(row);
  $('chat').scrollTop = $('chat').scrollHeight;
}
function suggest(list){
  $('chips').innerHTML = '';
  (list || []).forEach(c => {
    const b = make('button','sugg', esc(c));
    b.type = 'button';
    b.onclick = () => send(c);
    $('chips').append(b);
  });
}

function renderTurn(d){
  thinking(false);
  (d.history || []).forEach(h => {
    const key = h.role + '|' + h.text;
    if (S.seen.has(key)) return;
    S.seen.add(key);
    if (h.role === 'advisor' && h.text !== d.explanation &&
        (!d.question || h.text !== d.question.text)) say(h.text, 'them', 'sys');
  });

  if (d.gstin_check && !S.loggedGstin) {
    S.loggedGstin = true;
    const g = d.gstin_check;
    log(`GSTIN <code>${esc(g.input)}</code> — mod-36 checksum valid`, 'hit');
    if (g.verified){
      log(`Registry hit · status <b>${esc(g.status)}</b>`, 'hit');
      log(`Autofilled <b>${esc(g.record.legal_name)}</b>, ${esc(g.record.constitution)}, ${esc(g.record.state)}`, 'hit');
    } else {
      log(`Registry says <b>${esc(g.code.replace('GSTIN_',''))}</b> — GST-gated products withheld`, 'cut');
    }
  }

  if (d.finished){
    say(d.explanation, 'them');
    S.profile = d.profile; S.bundle = d.bundle;
    S.chosen = d.bundle.items.map(i => i.product_id);
    suggest([]);

    logHead('RECOMMENDATION');
    log(`Profile: ${esc(d.profile.business_type || 'business')}, ${d.profile.employee_count} staff, ` +
        `${d.profile.num_sites} site(s), tier ${d.profile.city_tier}`);
    log(`<b>${d.bundle.items.length}</b> products selected, <b>${d.bundle.excluded.length}</b> ruled out by catalogue rules`, 'hit');
    log(`Priced at <b>${inr(d.bundle.monthly_inr)}</b>/month · ready in ~${d.bundle.install_days} days`);
    railFoot('Products were chosen by the rules engine, not the language model.');

    const go = make('div','act');
    go.innerHTML = '<button class="primary" type="button">See the price</button>';
    go.querySelector('button').onclick = openQuote;
    $('chat').append(go);
    $('chat').scrollTop = $('chat').scrollHeight;
  } else {
    S.seen.add('advisor|' + d.question.text);
    say(d.question.text, 'them');
    suggest(d.question.chips);
  }
  S.sid = d.session_id;
}

async function send(text){
  if (!text.trim() || S.busy || !S.sid) return;
  S.busy = true;
  S.seen.add('owner|' + text);
  say(text, 'self');
  $('input').value = '';
  suggest([]);
  thinking(true);
  try { renderTurn(await post('/api/advisor/message', { session_id:S.sid, message:text })); }
  catch { thinking(false); say('That did not go through. Try again.', 'them'); }
  S.busy = false;
  $('input').focus();
}

async function restart(){
  $('chat').innerHTML = '';
  $('log').innerHTML = '';
  suggest([]);
  S.sid = null; S.seen = new Set(); S.order = null;
  S.applicant = 'K03'; S.loggedGstin = false;
  S.grants = { identity_verification:false, service_delivery:false,
               service_updates:true, marketing:false };
  show('chat');
  logHead('SESSION');
  const health = await get('/api/health').catch(() => null);
  if (health) log(`Loaded <b>${health.counts.products}</b> products, ` +
                  `<b>${health.counts.gstin_records.toLocaleString('en-IN')}</b> GST records, ` +
                  `<b>${health.counts.udyam_records.toLocaleString('en-IN')}</b> Udyam records`, 'hit');
  railFoot('Ask the advisor anything. Hindi and Hinglish both work.');
  renderTurn(await post('/api/advisor/start'));
}

/* ═══════════════ 2. the bill ═══════════════ */
async function openQuote(){
  show('quote');
  await priceIt();
}

async function priceIt(){
  const d = await post('/api/quote/reprice', { session_id:S.sid, product_ids:S.chosen });
  S.bundle = d.bundle; S.profile = d.profile; S.addons = d.addons || [];
  S.chosen = d.bundle.items.map(i => i.product_id);
  drawQuote();
}

function drawQuote(){
  const b = S.bundle, saved = b.monthly_saving_inr || 0;
  const root = $('view-quote');
  root.innerHTML = '';

  root.append(make('div','bill-top', `
    <div class="bill-cap">Your monthly price, everything included</div>
    <div class="figure"><b>${inr(b.monthly_inr)}</b><span>per month</span></div>
    ${b.setup_inr ? `<div class="figure-sub">${inr(b.setup_inr)} once, to set up</div>` : ''}
    ${saved > 0
      ? `<div class="badge">You save ${inr(saved)} a month on ${inr(b.current_spend_inr)} today</div>`
      : `<div class="figure-sub">One bill from one company, not five</div>`}
    <div class="terms"><span><b>${b.contract_months}</b> month plan</span>
      <span>Working in about <b>${b.install_days}</b> days</span></div>`));

  const got = make('div','panel');
  got.append(make('div','panel-h', 'What you get'));
  const gotBody = make('div','panel-b');
  b.items.forEach(i => {
    const row = make('div','entry', `
      <div class="entry-name">${esc(i.name)}${i.quantity > 1 ? ` <em>× ${i.quantity}</em>` : ''}</div>
      <div class="entry-sum">${inr(i.monthly_inr)}</div>
      <div class="entry-why">${esc(i.reason)}</div>
      <div class="entry-act">${i.is_addon
        ? '<button class="drop" type="button">Remove</button>'
        : '<span class="locked">Core</span>'}</div>`);
    const btn = row.querySelector('button');
    if (btn) btn.onclick = () => {
      S.chosen = S.chosen.filter(p => p !== i.product_id);
      log(`Removed ${esc(i.name)} · basket revalidated`, 'cut');
      priceIt();
    };
    gotBody.append(row);
  });
  got.append(gotBody);
  root.append(got);

  (b.notes || []).forEach(n => root.append(make('div','told', esc(n))));

  if (S.addons.length){
    const more = make('div','panel');
    more.append(make('div','panel-h', 'Add anything else'));
    const body = make('div','panel-b');
    S.addons.slice(0,6).forEach(a => {
      const row = make('div','entry', `
        <div class="entry-name">${esc(a.name)}${a.quantity > 1 ? ` <em>× ${a.quantity}</em>` : ''}</div>
        <div class="entry-sum">${inr(a.monthly_inr)}</div>
        <div class="entry-why">${esc(a.reason)}</div>
        <div class="entry-act"><button class="pick" type="button">Add</button></div>`);
      row.querySelector('button').onclick = () => {
        S.chosen.push(a.product_id);
        log(`Added ${esc(a.name)} · eligibility re-checked`, 'hit');
        priceIt();
      };
      body.append(row);
    });
    more.append(body);
    root.append(more);
  }

  if ((b.excluded || []).length){
    const d = make('details','panel');
    d.append(make('summary', null,
      `Why ${b.excluded.length} other product${b.excluded.length > 1 ? 's were' : ' was'} not offered`));
    const ul = make('ul');
    b.excluded.slice(0,10).forEach(e => ul.append(make('li', null, esc(e.reason))));
    d.append(ul);
    root.append(d);
  }

  const act = make('div','act');
  act.innerHTML = `<button class="primary" type="button">Continue · ${inr(b.monthly_inr)} a month</button>
    <div class="calm">Nothing is charged yet</div>`;
  act.querySelector('button').onclick = openConsent;
  root.append(act);

  railFoot('Every add or remove re-runs the full eligibility check across all 25 products. ' +
           'An ineligible one cannot enter the basket.');
}

/* ═══════════════ 3. permissions ═══════════════ */
async function openConsent(){
  show('consent');
  const notice = await get('/api/consent/notice');
  const root = $('view-consent');
  root.innerHTML = '';

  logHead('PERMISSIONS');
  log(`${notice.purposes.length} purposes offered separately, ` +
      `${notice.purposes.filter(p => p.required).length} required`);

  root.append(make('div','intro', `
    <h2>What we need, and why</h2>
    <p>Each permission is separate. The two we need to verify your business are marked;
       the rest are yours to refuse without changing the service.</p>`));

  notice.purposes.forEach(p => {
    const card = make('div','perm' + (S.grants[p.key] ? ' on' : ''));
    card.innerHTML = `
      <label class="perm-h">
        <input type="checkbox" ${S.grants[p.key] ? 'checked' : ''}>
        <span class="perm-t">${esc(p.title)}<span class="${p.required ? 'must' : 'may'}">${
          p.required ? 'Needed' : 'Optional'}</span></span>
      </label>
      <p class="perm-body">${esc(p.notice)}</p>
      <div class="perm-meta"><b>We use:</b> ${p.data.map(esc).join(', ')}</div>
      <div class="perm-meta"><b>We keep it:</b> ${esc(p.retention)}</div>`;
    const box = card.querySelector('input');
    box.onchange = e => {
      S.grants[p.key] = e.target.checked;
      card.classList.toggle('on', e.target.checked);
      log(`${e.target.checked ? 'Granted' : 'Refused'} — ${esc(p.title.toLowerCase())}`,
          e.target.checked ? 'hit' : 'cut');
      gateConsent();
    };
    root.append(card);
  });

  const rights = make('details','panel');
  rights.append(make('summary', null, 'What you can ask us to do with this data'));
  const ul = make('ul');
  notice.rights.forEach(r => ul.append(make('li', null, esc(r))));
  ul.append(make('li', null, `Write to ${esc(notice.grievance_officer)}`));
  rights.append(ul);
  root.append(rights);

  const act = make('div','act');
  act.innerHTML = `<button class="primary" id="consent-go" type="button">Agree and continue</button>
    <div class="calm" id="consent-say"></div>`;
  act.querySelector('button').onclick = openKyc;
  root.append(act);
  gateConsent();
  railFoot('Checkout is blocked server-side until both required permissions are given.');
}

function gateConsent(){
  const missing = ['identity_verification','service_delivery'].filter(k => !S.grants[k]);
  const btn = $('consent-go');
  if (!btn) return;
  btn.disabled = missing.length > 0;
  $('consent-say').textContent = missing.length
    ? 'We cannot verify your business without the two needed permissions'
    : 'You can switch the optional ones off later, any time';
}

/* ═══════════════ 4. verification ═══════════════ */
const DOC_LABEL = {
  gst_certificate:'GST certificate', udyam_certificate:'Udyam certificate',
  address_proof:'Address proof', signatory_id:'Signatory ID',
};

const DOC_ORDER = ['gst_certificate','udyam_certificate','address_proof','signatory_id'];
const DOC_HINT = {
  gst_certificate:'The certificate from the GST portal',
  udyam_certificate:'From udyamregistration.gov.in',
  address_proof:'An electricity bill from the last 90 days',
  signatory_id:'ID of whoever signs for the business',
};

function openKyc(){
  show('kyc');
  const root = $('view-kyc');
  root.innerHTML = '';
  S.files = {};

  root.append(make('div','intro', `
    <h2>Verify your business</h2>
    <p>Photograph or upload four documents. We read them and check your GST and
       Udyam numbers against the government registries. There are no forms to fill.</p>`));

  /* The real journey: the owner's own documents. */
  const up = make('div','panel');
  up.append(make('div','panel-h','Your documents'));
  const upBody = make('div','panel-b');
  DOC_ORDER.forEach(k => {
    const tile = make('label','tile', `
      <input type="file" accept="image/*,application/pdf" hidden>
      <span class="tile-mark" aria-hidden="true">+</span>
      <span class="tile-text">
        <span class="tile-name">${DOC_LABEL[k]}</span>
        <span class="tile-hint">${DOC_HINT[k]}</span>
      </span>`);
    tile.querySelector('input').onchange = e => {
      const f = e.target.files[0];
      if (!f) return;
      S.files[k] = f;
      tile.classList.add('filled');
      tile.querySelector('.tile-mark').textContent = '✓';
      tile.querySelector('.tile-hint').textContent = f.name;
      log(`Attached ${esc(f.name)} as ${DOC_LABEL[k].toLowerCase()}`);
      gateUpload();
    };
    upBody.append(tile);
  });
  up.append(upBody);
  root.append(up);

  const upAct = make('div','act');
  upAct.innerHTML = `<button class="primary" id="verify-mine" type="button" disabled>Verify my documents</button>
    <div class="calm" id="upload-say">Add your documents to continue</div>`;
  upAct.querySelector('button').onclick = () => {
    const fd = new FormData();
    Object.entries(S.files).forEach(([k,f]) => fd.append(k, f));
    logHead('VERIFYING UPLOAD');
    runVerification(
      fetch('/api/kyc/upload', { method:'POST', body:fd }).then(r => r.json()),
      Object.keys(S.files));
  };
  root.append(upAct);

  /* Demo scaffolding, labelled as such so it never reads as part of the
     customer journey. A real owner does not choose who they are. */
  const demo = make('details','panel');
  demo.append(make('summary', null, 'Demo shortcut — load one of the 12 specimen applicants'));
  const demoBody = make('div','panel-b');
  demoBody.append(make('div','note-line',
    'Not part of the customer journey. These ship with the hackathon data pack, and ' +
    'several carry deliberate faults so the checks can be demonstrated.'));
  const grid = make('div','picker');
  for (let i = 1; i <= 12; i++){
    const id = 'K' + String(i).padStart(2,'0');
    const b = make('button','pick-one', id);
    b.type = 'button';
    b.onclick = () => {
      S.applicant = id;
      [...grid.children].forEach(c => c.setAttribute('aria-pressed', String(c === b)));
      logHead('VERIFYING ' + id);
      runVerification(get('/api/kyc/verify/' + id), DOC_ORDER);
    };
    grid.append(b);
  }
  demoBody.append(grid);
  demo.append(demoBody);
  root.append(demo);

  /* Shared output, filled by runVerification. */
  const out = make('div'); out.id = 'kyc-out'; root.append(out);
  const live = make('div','scan'); live.id = 'kyc-scan'; live.hidden = true; root.append(live);
  const detail = make('div'); detail.id = 'kyc-detail'; root.append(detail);
  const slot = make('div'); slot.id = 'kyc-act'; root.append(slot);

  railFoot('Upload your own certificates, or load a specimen to watch the checks run.');
}

function gateUpload(){
  const n = Object.keys(S.files || {}).length;
  const btn = $('verify-mine');
  if (!btn) return;
  btn.disabled = n === 0;
  $('upload-say').textContent = n === 0
    ? 'Add your documents to continue'
    : n < 4 ? `${n} of 4 added — we will tell you what is still missing`
            : 'All four added';
}

/* Walks the beacons while the real request is in flight. The work genuinely is
   happening: OCR across four images takes a couple of seconds. */
async function runVerification(request, provided){
  const live = $('kyc-scan');
  live.hidden = false;
  live.innerHTML = '';
  DOC_ORDER.forEach(k => {
    live.append(make('div','scan-row wait', `
      <span class="beacon" data-doc="${k}"></span>
      <span class="scan-name">${DOC_LABEL[k]}</span>
      <span class="scan-val" data-val="${k}">${provided.includes(k) ? 'queued' : 'not provided'}</span>`));
  });
  $('kyc-out').innerHTML = '';
  $('kyc-detail').innerHTML = '';
  $('kyc-act').innerHTML = '';

  (async () => {
    for (const k of DOC_ORDER){
      if (!provided.includes(k)) continue;
      const dot = live.querySelector(`[data-doc="${k}"]`);
      if (!dot) return;
      dot.classList.add('run');
      dot.closest('.scan-row').classList.remove('wait');
      live.querySelector(`[data-val="${k}"]`).textContent = 'reading';
      await wait(380);
    }
  })();

  let report;
  try { report = await request; }
  catch {
    $('kyc-out').innerHTML =
      '<div class="panel"><div class="panel-b">Could not read those documents. Try again.</div></div>';
    return;
  }

  DOC_ORDER.forEach(k => {
    const dot = live.querySelector(`[data-doc="${k}"]`);
    const val = live.querySelector(`[data-val="${k}"]`);
    const d = report.documents[k];
    dot.classList.remove('run');
    dot.closest('.scan-row').classList.remove('wait');
    if (!d){ dot.classList.add('no'); val.textContent = 'not uploaded';
             log(`${DOC_LABEL[k]} — <b>missing</b>`, 'cut'); }
    else if (!d.legible){ dot.classList.add('no'); val.textContent = `sharpness ${d.blur}`;
             log(`${DOC_LABEL[k]} — sharpness <b>${d.blur}</b>, refused before OCR`, 'cut'); }
    else if (d.deskewed){ dot.classList.add('fix'); val.textContent = `straightened ${Math.abs(d.skew).toFixed(0)}°`;
             log(`${DOC_LABEL[k]} — skew <b>${Math.abs(d.skew).toFixed(0)}°</b>, straightened then read`, 'fix'); }
    else { dot.classList.add('ok'); val.textContent = `sharpness ${d.blur}`;
           log(`${DOC_LABEL[k]} — read cleanly`, 'hit'); }
  });

  drawVerdict(report, $('kyc-out'));
}


function drawVerdict(r, out){
  out.innerHTML = '';
  const tone = r.status === 'AUTO_APPROVED' ? 'pass' : 'hold';
  out.append(make('div','verdict ' + tone, `
    <div class="verdict-k">${esc(r.status.replace(/_/g,' '))}</div>
    <div class="verdict-h">${esc(r.headline)}</div>`));

  const problems = r.findings.filter(f => f.severity !== 'info');
  const handled  = r.findings.filter(f => f.severity === 'info');

  problems.forEach(f => log(esc(f.message), f.severity === 'block' ? 'cut' : 'fix'));
  if (!problems.length) log('All checks passed — approved without a human', 'hit');

  if (problems.length){
    const p = make('div','panel');
    p.append(make('div','panel-h', problems.length === 1 ? 'One thing to fix'
                                                         : `${problems.length} things to fix`));
    const body = make('div','panel-b');
    problems.forEach(f => body.append(make('div','issue', `
      <div class="issue-what">${esc(f.message)}</div>
      ${f.action ? `<div class="issue-fix">${esc(f.action)}</div>` : ''}
      <div class="issue-ref">${esc(f.code)}</div>`)));
    p.append(body);
    out.append(p);
  }

  if (handled.length){
    const p = make('div','panel');
    p.append(make('div','panel-h','Fixed for you'));
    const body = make('div','panel-b');
    handled.forEach(f => body.append(make('div','issue',
      `<div class="issue-what">${esc(f.message)}</div>`)));
    p.append(body);
    out.append(p);
  }

  const detail = $('kyc-detail');
  detail.innerHTML = '';

  if (Object.keys(r.autofill || {}).length){
    const p = make('div','panel');
    p.append(make('div','panel-h','Taken from the registries'));
    const body = make('div','panel-b');
    body.append(make('div','note-line','You did not type any of this.'));
    Object.entries(r.autofill).forEach(([k,v]) => body.append(make('div','pair',
      `<span>${esc(k.replace(/_/g,' '))}</span><b>${esc(v)}</b>`)));
    p.append(body);
    detail.append(p);
    log(`<b>${Object.keys(r.autofill).length}</b> fields autofilled from GST and Udyam`, 'hit');
  }

  if (Object.keys(r.extracted || {}).length){
    const p = make('div','panel');
    p.append(make('div','panel-h','Identifiers, masked'));
    const body = make('div','panel-b');
    Object.entries(r.extracted).forEach(([k,v]) => body.append(make('div','pair',
      `<span>${esc(k.replace(/_/g,' '))}</span><b class="mono">${esc(v)}</b>`)));
    p.append(body);
    detail.append(p);
  }

  const act = make('div','act');
  if (r.status === 'AUTO_APPROVED'){
    act.innerHTML = `<button class="primary" type="button">Confirm order</button>
      <div class="calm">Verified in seconds. Nothing was posted or emailed.</div>`;
    act.querySelector('button').onclick = placeOrder;
    railFoot('Approved automatically. No human touched this application.');
  } else {
    act.innerHTML = `<button class="secondary" type="button">Try another applicant</button>
      <div class="calm">This cannot go ahead until the above is resolved</div>`;
    act.querySelector('button').onclick = () => $('view-kyc').scrollTo({top:0,behavior:'smooth'});
    railFoot('Held back with a specific reason, not a generic rejection.');
  }
  const slot = $('kyc-act');
  slot.innerHTML = '';
  slot.append(act);
}

/* ═══════════════ 5. install ═══════════════ */
async function placeOrder(){
  const res = await post('/api/checkout', {
    session_id:S.sid, product_ids:S.chosen, consents:S.grants,
    applicant:S.applicant, contact_name:S.profile?.business_type || '',
  });
  if (!res.ok){ alert(res.message || 'Could not place the order'); return; }
  S.order = res.order;
  logHead('ORDER PLACED');
  log(`<b>${esc(res.order.order_id)}</b> created · live by ${esc(res.order.expected_live)}`, 'hit');
  log(`${res.order.consent.records.length} consent records written to the ledger`, 'hit');
  drawTracker();
  show('tracker');
}

function drawTracker(){
  const o = S.order, root = $('view-tracker');
  root.innerHTML = '';

  const pushes = o.consent.records.some(c => c.purpose === 'service_updates' && c.granted);
  root.append(make('div','run-top', `
    <div class="run-id">${esc(o.order_id)}</div>
    <div class="run-state">${esc(o.status)}</div>
    <div class="run-say">${esc(o.status_detail)}</div>
    <div class="meter"><i style="width:${o.progress_pct}%"></i></div>
    <div class="terms"><span><b>${inr(o.monthly_inr)}</b> a month</span>
      <span>${o.is_live ? 'Live now' : `Live by <b>${esc(o.expected_live)}</b>`}</span></div>
    ${pushes && !o.is_live
      ? '<div class="ping">We will message you at every step. There is no need to call or check back.</div>'
      : ''}`));

  const steps = make('div','panel');
  steps.append(make('div','panel-h','Where your order is'));
  const body = make('div','panel-b');
  const LEG = { done:'done', current:'now', upcoming:'soon' };
  o.timeline.forEach(s => body.append(make('div','leg ' + (LEG[s.state] || 'soon'), `
    <div class="leg-dot"></div>
    <div><div class="leg-t">${esc(s.label)}</div>
      <div class="leg-d">${esc(s.detail)}</div>
      <div class="leg-on">${esc(s.on)}</div></div>`)));
  steps.append(body);
  root.append(steps);

  const perms = make('div','panel');
  perms.append(make('div','panel-h','Your permissions'));
  const pb = make('div','panel-b');
  pb.append(make('div','note-line','Switch off anything optional, any time.'));
  o.consent.records.forEach(c => {
    const row = make('div','pair', `<span>${esc(c.purpose_title)}</span>`);
    const right = make('b');
    if (c.granted && c.required){
      right.append(make('span','state-on','On'),
                   make('span','state-note','needed for the service'));
    } else if (c.granted){
      right.append(make('span','state-on','On'), document.createTextNode(' '));
      const btn = make('button','drop','Switch off');
      btn.type = 'button';
      btn.onclick = async () => {
        const res = await post(`/api/orders/${o.order_id}/consent/withdraw`, { purpose:c.purpose });
        if (!res.ok){ alert(res.message); return; }
        log(`Withdrew — ${esc(c.purpose_title.toLowerCase())} · ledger appended, not erased`, 'fix');
        S.order = await get('/api/orders/' + o.order_id);
        drawTracker();
      };
      right.append(btn);
    } else {
      right.append(make('span','state-off', c.withdrawn_at ? 'Withdrawn' : 'Off'));
    }
    row.append(right);
    pb.append(row);
  });
  perms.append(pb);
  root.append(perms);

  /* Demo controls, kept apart from the journey. The owner cannot move their
     own installation along — that comes from the operator's systems — so
     these simulate the operator's side rather than pretending to be a
     customer action. */
  const demo = make('details','panel');
  demo.append(make('summary', null, 'Demo controls'));
  const dBody = make('div','panel-b');
  dBody.append(make('div','note-line',
    'Not shown to the owner. Progress normally arrives from provisioning and ' +
    'field systems; these stand in for those events.'));
  if (!o.is_live){
    const next = make('button','secondary','Simulate the next operator update');
    next.type = 'button';
    next.onclick = async () => {
      S.order = await post(`/api/orders/${o.order_id}/advance`);
      log(`Operator update — order now at <b>${esc(S.order.status)}</b>`);
      drawTracker();
      $('view-tracker').querySelector('details')?.setAttribute('open','');
    };
    dBody.append(next);
  }
  const again = make('button','secondary','Start a new journey');
  again.type = 'button';
  again.onclick = restart;
  dBody.append(again);
  demo.append(dBody);
  root.append(demo);

  root.append(make('div','act', '<div class="calm">' +
    (o.is_live
      ? 'Your connection is live. Nothing further is needed.'
      : 'Nothing for you to do. This page updates itself as the work progresses.') +
    '</div>'));

  railFoot(o.is_live
    ? 'Three chase calls avoided. In the source data, five chases meant 28% churn.'
    : 'Dates come from the catalogue’s own install times, not a guess.');
}

/* ─────────────── boot ─────────────── */
$('composer').onsubmit = e => { e.preventDefault(); send($('input').value); };
$('restart').onclick = restart;
$('back').onclick = () => {
  const at = SCREENS.find(s => !$('view-' + s).hidden);
  show({ quote:'chat', consent:'quote', kyc:'consent' }[at] || 'chat');
};

(async () => {
  try {
    const s = await get('/api/llm/status');
    $('status').textContent = s.enabled ? `ready · ${s.model}`
      : (s.configured ? 'ready · offline mode' : 'ready');
  } catch { $('status').textContent = 'ready'; }
  restart();
})();
