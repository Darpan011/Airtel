"""Project Udyam Digital — FastAPI application entry point.

Airtel SYNC 2026, Enterprise Track A.
A digital-first acquisition and onboarding journey for Indian MSMEs.
"""
import re
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import advisor as advisor_service
from .config import KYC_DIR, STATIC_DIR, TODAY
from .data import get_store
from .consent import PURPOSES, ConsentLedger, consent_notice
from .engine import available_addons, recommend, reprice
from .orders import create_order, get_order
from .kyc import (
    DocumentRead,
    read_document,
    tesseract_path,
    validate_application,
    verify_specimen,
)
from .llm import llm_status
from .profile import BusinessProfile
from .registry import gstin_lookup, udyam_lookup

app = FastAPI(
    title="Project Udyam Digital",
    description="Digital-first MSME acquisition and onboarding — Airtel SYNC 2026, Track A",
    version="0.1.0",
)


@app.on_event("startup")
def _warm_cache() -> None:
    """Parse the CSVs once at boot so the first request is not slow."""
    store = get_store()
    print(
        f"[udyam] loaded {len(store.products)} products, "
        f"{len(store.gstin_registry)} GSTIN records, "
        f"{len(store.udyam_registry)} Udyam records, "
        f"{len(store.leads)} leads"
    )


# ----------------------------------------------------------------------
# Health and metadata
# ----------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict:
    store = get_store()
    return {
        "status": "ok",
        "today": TODAY.isoformat(),
        "counts": {
            "products": len(store.products),
            "gstin_records": len(store.gstin_registry),
            "udyam_records": len(store.udyam_registry),
            "leads": len(store.leads),
        },
    }


# ----------------------------------------------------------------------
# Mock registry APIs — these stand in for GSTN and the Udyam portal
# ----------------------------------------------------------------------
@app.get("/api/gstin/{gstin}")
def api_gstin(gstin: str) -> dict:
    return gstin_lookup(gstin)


@app.get("/api/udyam/{number}")
def api_udyam(number: str) -> dict:
    return udyam_lookup(number)


# ----------------------------------------------------------------------
# Product catalogue
# ----------------------------------------------------------------------
@app.get("/api/products")
def api_products() -> dict:
    return {"products": get_store().all_products()}


@app.get("/api/products/{product_id}")
def api_product(product_id: str) -> dict:
    product = get_store().product(product_id)
    if product is None:
        raise HTTPException(status_code=404, detail=f"No product {product_id}")
    return product


# ----------------------------------------------------------------------
# Advisor — the conversational journey
# ----------------------------------------------------------------------
class MessageIn(BaseModel):
    session_id: str
    message: str


@app.post("/api/advisor/start")
def api_advisor_start() -> dict:
    return advisor_service.start_session()


@app.post("/api/advisor/message")
def api_advisor_message(payload: MessageIn) -> dict:
    try:
        return advisor_service.send_message(payload.session_id, payload.message)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown or expired session")


@app.get("/api/advisor/{session_id}")
def api_advisor_get(session_id: str) -> dict:
    session = advisor_service.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown or expired session")
    if session.finished:
        return advisor_service.result_payload(session)
    return {
        "session_id": session.session_id,
        "question": session.question(),
        "history": session.history,
        "finished": False,
        "slots": session.slots,
    }


# ----------------------------------------------------------------------
# Direct recommendation (used by the quote screen and for testing)
# ----------------------------------------------------------------------
@app.post("/api/recommend")
def api_recommend(profile: dict) -> dict:
    return recommend(BusinessProfile.from_dict(profile))


@app.get("/api/llm/status")
def api_llm_status() -> dict:
    return llm_status()


# ----------------------------------------------------------------------
# Smart KYC
# ----------------------------------------------------------------------
@app.get("/api/kyc/specimens")
def api_kyc_specimens() -> dict:
    """The 12 specimen applicants shipped with the data pack."""
    available = sorted({p.name.split("_")[0] for p in KYC_DIR.glob("K*_*.png")})
    return {"applicants": available, "ocr_ready": tesseract_path() is not None}


@app.get("/api/kyc/status")
def api_kyc_status() -> dict:
    """Tells you whether OCR will work before you try to use it."""
    binary = tesseract_path()
    return {
        "ocr_ready": binary is not None,
        "tesseract": binary or "not found",
        "documents_found": len(list(KYC_DIR.glob("K*_*.png"))),
    }


@app.get("/api/kyc/verify/{applicant}")
def api_kyc_verify(applicant: str) -> dict:
    """Run the full KYC pipeline over one specimen applicant."""
    if not re.fullmatch(r"K\d{2}", applicant or ""):
        raise HTTPException(status_code=400, detail="Applicant must look like K01")
    if not list(KYC_DIR.glob(f"{applicant}_*.png")):
        raise HTTPException(status_code=404, detail=f"No documents for {applicant}")
    return verify_specimen(applicant)


@app.post("/api/kyc/upload")
async def api_kyc_upload(
    gst_certificate: UploadFile | None = File(None),
    udyam_certificate: UploadFile | None = File(None),
    address_proof: UploadFile | None = File(None),
    signatory_id: UploadFile | None = File(None),
) -> dict:
    """Verify a real upload.

    Documents are written to a temporary directory and deleted as soon as the
    fields have been extracted and checked. Nothing about the image is
    retained: DPDP storage limitation enforced by the code path, not by a
    retention policy nobody runs.
    """
    uploads = {
        "gst_certificate": gst_certificate,
        "udyam_certificate": udyam_certificate,
        "address_proof": address_proof,
        "signatory_id": signatory_id,
    }
    docs: dict[str, DocumentRead] = {}
    with tempfile.TemporaryDirectory() as workdir:
        for doc_type, upload in uploads.items():
            if upload is None or not upload.filename:
                continue
            target = Path(workdir) / f"{doc_type}{Path(upload.filename).suffix or '.png'}"
            target.write_bytes(await upload.read())
            docs[doc_type] = read_document(target, doc_type)
    # The TemporaryDirectory is gone by here; only extracted fields survive.
    return validate_application(docs)


# ----------------------------------------------------------------------
# Quote editing
# ----------------------------------------------------------------------
class RepriceIn(BaseModel):
    session_id: str
    product_ids: list[str]


@app.post("/api/quote/reprice")
def api_reprice(payload: RepriceIn) -> dict:
    """Re-cost an owner-edited basket, re-running every eligibility rule."""
    session = advisor_service.get_session(payload.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown or expired session")
    profile = session.to_profile()
    result = reprice(profile, payload.product_ids)
    result["addons"] = available_addons(
        profile, [i["product_id"] for i in result["bundle"]["items"]])
    return result


@app.get("/api/quote/{session_id}/addons")
def api_addons(session_id: str) -> dict:
    session = advisor_service.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown or expired session")
    profile = session.to_profile()
    current = [i["product_id"] for i in recommend(profile)["bundle"]["items"]]
    return {"addons": available_addons(profile, current)}


# ----------------------------------------------------------------------
# DPDP consent
# ----------------------------------------------------------------------
@app.get("/api/consent/notice")
def api_consent_notice() -> dict:
    return consent_notice()


# ----------------------------------------------------------------------
# Checkout and tracker
# ----------------------------------------------------------------------
class CheckoutIn(BaseModel):
    session_id: str
    product_ids: list[str] = []
    consents: dict[str, bool] = {}
    applicant: str | None = None        # specimen K01-K12, for the demo
    contact_name: str = ""
    contact_mobile: str = ""


@app.post("/api/checkout")
def api_checkout(payload: CheckoutIn) -> dict:
    """Place an order: validate consent, run KYC, then create the order."""
    session = advisor_service.get_session(payload.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown or expired session")

    ledger = ConsentLedger()
    for purpose in PURPOSES:
        ledger.record(purpose, bool(payload.consents.get(purpose)))

    # No required consent, no processing. This is the whole point of the Act.
    if ledger.missing_required():
        return {
            "ok": False,
            "reason": "CONSENT_REQUIRED",
            "message": "We cannot verify your business or set up your connection "
                       "without the two required permissions.",
            "missing": [PURPOSES[k]["title"] for k in ledger.missing_required()],
            "consent": ledger.to_dict(),
        }

    profile = session.to_profile()
    ids = payload.product_ids or [i["product_id"]
                                  for i in recommend(profile)["bundle"]["items"]]
    quote = reprice(profile, ids)

    kyc_result = None
    if payload.applicant:
        if not re.fullmatch(r"K\d{2}", payload.applicant):
            raise HTTPException(status_code=400, detail="Applicant must look like K01")
        kyc_result = verify_specimen(payload.applicant)
        if kyc_result["status"] != "AUTO_APPROVED":
            return {
                "ok": False,
                "reason": "KYC_INCOMPLETE",
                "message": kyc_result["headline"],
                "kyc": kyc_result,
                "consent": ledger.to_dict(),
            }

    order = create_order(
        profile=quote["profile"], bundle=quote["bundle"], consent=ledger,
        kyc=kyc_result, contact_name=payload.contact_name,
        contact_mobile=payload.contact_mobile,
    )
    return {"ok": True, "order": order.to_dict(), "otp_hint": order.otp}


@app.get("/api/orders/{order_id}")
def api_order(order_id: str) -> dict:
    order = get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="No such order")
    return order.to_dict()


@app.post("/api/orders/{order_id}/advance")
def api_order_advance(order_id: str) -> dict:
    """Demo helper: move the order to its next milestone."""
    order = get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="No such order")
    order.advance()
    return order.to_dict()


class WithdrawIn(BaseModel):
    purpose: str


@app.post("/api/orders/{order_id}/consent/withdraw")
def api_withdraw(order_id: str, payload: WithdrawIn) -> dict:
    """Withdrawing consent must be as easy as giving it (DPDP s.6(4))."""
    order = get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="No such order")
    if payload.purpose not in PURPOSES:
        raise HTTPException(status_code=400, detail="Unknown purpose")
    if PURPOSES[payload.purpose]["required"]:
        return {
            "ok": False,
            "message": "This permission is needed to keep your connection running. "
                       "To withdraw it you would need to close the service.",
            "consent": order.consent.to_dict(),
        }
    order.consent.withdraw(payload.purpose)
    return {"ok": True, "consent": order.consent.to_dict()}


# ----------------------------------------------------------------------
# Static front end
# ----------------------------------------------------------------------
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
