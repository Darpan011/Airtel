# Project Udyam Digital

**Airtel SYNC 2026 · Enterprise Track A**

A digital-first acquisition and onboarding journey that lets an Indian small-business
owner discover the right bundle, buy it, and get it activated on a phone, in their own
language, with no salesperson involved.

Built against the synthetic Accel Business data pack: 25 products, 5,000 leads,
3,534 GSTIN records, 2,723 Udyam records and 47 specimen KYC documents.

---

## The problem, in one table

| Today | Target |
|---|---|
| 7.4% of leads activate | 15% |
| 17.6 days lead to activation | under 5 days |
| ₹15,053 to acquire one SMB | under ₹5,000 |
| 3 status-chasing calls per customer | under 0.5 |
| 15.2% churn at 6 months | under 10% |

Every figure on the left is measured from `smb_leads.csv`, not assumed.

---

## Quick start

Requires **Python 3.10+** and **Tesseract OCR**
([Windows installer](https://github.com/UB-Mannheim/tesseract/wiki) ·
`brew install tesseract` · `sudo apt install tesseract-ocr`).

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

Open <http://127.0.0.1:8000>.

Optional: put a free [Google AI Studio](https://aistudio.google.com/apikey) key in `.env`
as `GEMINI_API_KEY=...`. **The journey works fully without one** — the advisor falls back
to deterministic parsing and templated explanations, so a demo never depends on a live
network call.

Check `/api/kyc/status` to confirm Tesseract was found.

---

## The journey

```
chat  →  quote  →  consent  →  KYC  →  tracker
```

**Advisor** — at most six questions, in English, Hindi, Hinglish or by tapping chips.
Paste a GSTIN and it verifies against the registry and fills in your business details.

**Quote** — one monthly price, add-ons you can toggle, every line item explained in plain
words, and an expandable list of *why other products were not offered*.

**Consent** — four separately tickable DPDP purposes, each with its data and retention
period. Checkout is blocked server-side until the two required ones are given.

**Smart KYC** — upload four documents; they are quality-gated, deskewed, OCR'd, validated
against the GSTIN and Udyam registries, and cross-checked for name, date and state
consistency. Every failure returns a specific reason and the fix.

**Tracker** — order status with milestone dates projected from `typical_install_days`, so
the owner never has to call support.

---

## The one architectural decision that matters

**The LLM never chooses products.**

```
Gemini          →  understands the owner's reply, writes the final explanation
BusinessProfile →  the structured contract between them
Rules engine    →  selects products, deterministically, from the catalogue rules
```

A free-form LLM recommender will eventually hallucinate an ineligible bundle. This one
cannot: eligibility, dependencies, quantities and pricing are pure Python over
`product_catalogue.csv`.

**Proof:** `tests/test_phase1.py` pushes all 5,000 leads through the engine and asserts
zero eligibility violations and zero unmet dependencies. For comparison, the human field
force in that same dataset sold **49 bundles that break the catalogue's own rules**.

---

## Tests

```bash
python tests/test_phase0.py     # data, GSTIN checksum, registries, masking   30
python tests/test_phase1.py     # rules engine + 5,000-lead invariant fuzz    39
python tests/test_phase2.py     # advisor: termination, Hindi, autofill       54
python tests/test_phase3.py     # Smart KYC across all 12 applicants          58
python tests/test_phase4.py     # quote editing, consent, checkout, tracker   52
```

**233 assertions.** Phase 3 takes ~2 minutes — it genuinely OCRs 47 images.

---

## Smart KYC results

Twelve specimen applicants, seven with deliberate faults. All classify correctly:

| Applicant | Verdict | Reason |
|---|---|---|
| K01 | MANUAL_REVIEW | GST registration **cancelled**, and the Udyam number is blank |
| K02 | ✅ AUTO_APPROVED | scan was rotated 7° — straightened automatically |
| K03 | ✅ AUTO_APPROVED | |
| K04 | FIX_AND_RESUBMIT | too blurred to read (sharpness **1** vs 937+ for every other document) |
| K05 | FIX_AND_RESUBMIT | Udyam certificate not uploaded |
| K06 | MANUAL_REVIEW | GSTIN checksum fails and no valid repair exists |
| K07 | MANUAL_REVIEW | certificate reads "Shri", the registry holds "Neelgiri" |
| K08–K10 | ✅ AUTO_APPROVED | |
| K11 | FIX_AND_RESUBMIT | address proof 257 days old, limit is 90 |
| K12 | ✅ AUTO_APPROVED | |

Straight-through rate **6/12 = 50%** on a deliberately hostile set.

K02 is the instructive one: its GSTIN was unreadable *only* because the page was crooked.
Without deskewing, a fully compliant customer gets rejected.

---

## Project layout

```
app/
  main.py         FastAPI routes
  advisor.py      six-question state machine
  llm.py          Gemini client + deterministic fallback
  engine.py       rules engine: eligibility, dependencies, pricing, ETA
  profile.py      BusinessProfile — the LLM ↔ engine contract
  kyc.py          quality gate, deskew, OCR, extraction, validation
  registry.py     mock GSTIN and Udyam lookup services
  validators.py   GSTIN mod-36 checksum, masking
  consent.py      DPDP consent ledger (append-only)
  orders.py       order state and tracker milestones
  data.py         loads the data pack once, O(1) lookups
static/           hand-written HTML/CSS/JS — no framework, no CDN, no build step
tests/            233 assertions
docs/             architecture diagram, privacy note
data/             the synthetic data pack
```

---

## Privacy

See `docs/privacy-note.pdf`. In short: no field exists for PAN, Aadhaar or date of birth
because bundle selection does not need them; uploaded documents are deleted the moment
extraction finishes; identifiers are masked in the UI and logs, with a test asserting no
raw GSTIN appears in any KYC report.

---

## Notes

- **No database.** Orders and sessions live in memory and are lost on restart. Every store
  sits behind an accessor, so swapping in Postgres touches only the storage layer.
- **"Today" is pinned to 2026-07-15**, as the data pack's README requires for date checks.
- **All data is synthetic.** GSTINs carry valid checksums but the 4th PAN character is
  always `X` and Udyam district codes are `00`, so no record can match a real entity.
