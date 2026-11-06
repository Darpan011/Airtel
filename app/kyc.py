"""Smart KYC — document intake, OCR, extraction and validation.

The pipeline, per document:

    quality gate  ->  deskew  ->  OCR  ->  field extraction  ->  repair

then, across the whole application:

    completeness -> identifier validation -> registry lookup -> status check
    -> cross-document name consistency -> document freshness -> state match

Every failure produces a specific reason code and a sentence that tells the
owner what to do about it. "Rejected" is never an acceptable answer: the data
shows 273 leads dropped at exactly this step, and a vague error is why.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from rapidfuzz import fuzz

from .config import (
    ADDRESS_PROOF_MAX_AGE_DAYS,
    GST_STATE_CODES,
    NAME_MATCH_THRESHOLD,
    TODAY,
    UDYAM_STATE_CODES,
)
from .data import get_store
from .validators import (
    gstin_checksum_ok,
    mask_gstin,
    mask_udyam,
    validate_gstin_format,
    validate_udyam_format,
)

# Laplacian variance below this means the scan is too soft to trust.
# Calibrated on the specimen set: the blurred document scores 1.0, every
# legible one scores above 930.
BLUR_THRESHOLD = 100.0

# Beyond this many degrees we rotate before OCR.
SKEW_THRESHOLD_DEG = 2.0

# Written mid-sentence ("the GST registration certificate is blurred"), so
# proper nouns stay capitalised and the rest stays lower case.
REQUIRED_DOCUMENTS = {
    "gst_certificate": "GST registration certificate",
    "udyam_certificate": "Udyam registration certificate",
    "address_proof": "address proof",
    "signatory_id": "authorised signatory ID",
}


def _sentence_case(label: str) -> str:
    """Capitalise for the start of a sentence without mangling 'GST'."""
    return label[0].upper() + label[1:] if label[:1].islower() else label

# OCR confusion maps, applied only where the GSTIN format forces a character
# class. This is structural correction, not guessing.
_TO_DIGIT = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "|": "1",
             "S": "5", "B": "8", "Z": "2", "G": "6", "T": "7", "A": "4"}
_TO_LETTER = {"0": "O", "1": "I", "5": "S", "8": "B", "2": "Z", "6": "G",
              "7": "T", "4": "A"}


# ======================================================================
# Image handling
# ======================================================================
def blur_score(gray: np.ndarray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def skew_angle(gray: np.ndarray) -> float:
    """Estimate page rotation from the minimum-area rect around the ink."""
    inverted = cv2.bitwise_not(gray)
    binary = cv2.threshold(inverted, 0, 255,
                           cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
    coords = np.column_stack(np.where(binary > 0))
    if coords.size == 0:
        return 0.0
    angle = cv2.minAreaRect(coords)[-1]
    # minAreaRect reports in (-90, 0]; an upright page comes back as -90 or 0.
    if angle < -45:
        angle += 90
    elif angle > 45:
        angle -= 90
    return float(angle)


def rotate(image: np.ndarray, degrees: float) -> np.ndarray:
    """Rotate about the centre, growing the canvas so no text is clipped."""
    h, w = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), degrees, 1.0)
    cos, sin = abs(matrix[0, 0]), abs(matrix[0, 1])
    new_w, new_h = int(h * sin + w * cos), int(h * cos + w * sin)
    matrix[0, 2] += new_w / 2 - w / 2
    matrix[1, 2] += new_h / 2 - h / 2
    return cv2.warpAffine(image, matrix, (new_w, new_h),
                          flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))


import shutil

# The Windows installer often does not add Tesseract to PATH, so fall back to
# the places it actually installs to before giving up.
_TESSERACT_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    "/opt/homebrew/bin/tesseract",
    "/usr/local/bin/tesseract",
    "/usr/bin/tesseract",
]


def tesseract_path() -> Optional[str]:
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in _TESSERACT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


class TesseractMissing(RuntimeError):
    """Raised with installation guidance rather than a bare FileNotFoundError."""


def run_tesseract(image: np.ndarray) -> str:
    binary = tesseract_path()
    if binary is None:
        raise TesseractMissing(
            "Tesseract OCR is not installed, or not on PATH. "
            "Windows: install from https://github.com/UB-Mannheim/tesseract/wiki "
            r"(default path C:\Program Files\Tesseract-OCR). "
            "macOS: brew install tesseract. Linux: sudo apt install tesseract-ocr."
        )
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        cv2.imwrite(tmp.name, image)
        result = subprocess.run([binary, tmp.name, "-", "--psm", "6"],
                                capture_output=True, text=True)
    return result.stdout


@dataclass
class DocumentRead:
    doc_type: str
    filename: str
    blur: float = 0.0
    skew: float = 0.0
    deskewed: bool = False
    legible: bool = True
    text: str = ""
    fields: dict = field(default_factory=dict)


def read_document(path: str | Path, doc_type: str) -> DocumentRead:
    """Quality-gate, deskew, OCR and parse a single document."""
    path = Path(path)
    out = DocumentRead(doc_type=doc_type, filename=path.name)

    image = cv2.imread(str(path))
    if image is None:
        out.legible = False
        return out

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    out.blur = blur_score(gray)
    out.skew = skew_angle(gray)

    # A blurred scan is rejected before OCR. Running OCR on it produces
    # confident nonsense, which is far more dangerous than refusing to read.
    if out.blur < BLUR_THRESHOLD:
        out.legible = False
        return out

    if abs(out.skew) > SKEW_THRESHOLD_DEG:
        candidate = rotate(image, -out.skew)
        text = run_tesseract(candidate)
        # minAreaRect does not tell us the sign reliably; if the straightened
        # page does not parse, try the other direction before giving up.
        if not _looks_parsed(text, doc_type):
            other = run_tesseract(rotate(image, out.skew))
            if _looks_parsed(other, doc_type):
                text = other
        out.deskewed = True
        out.text = text
    else:
        out.text = run_tesseract(image)

    out.fields = parse_fields(doc_type, out.text)
    return out


def _looks_parsed(text: str, doc_type: str) -> bool:
    anchors = {
        "gst_certificate": "GSTIN",
        "udyam_certificate": "Enterprise name",
        "address_proof": "Billed to",
        "signatory_id": "Designation",
    }
    return anchors.get(doc_type, "") in text


# ======================================================================
# Field extraction
# ======================================================================
def _label(text: str, label: str) -> str:
    """Value on the same line as a label, else the next non-empty line."""
    match = re.search(rf"{label}\s*[:\-]?\s*(.*)", text, re.IGNORECASE)
    if not match:
        return ""
    value = match.group(1).strip()
    if value:
        return value
    tail = text[match.end():].lstrip("\n")
    return tail.split("\n")[0].strip() if tail else ""


def repair_gstin(raw: str) -> str:
    """Snap an OCR'd GSTIN onto the character classes the format requires.

    Positions 0-1 and 7-10 must be digits; 2-6 and 11 must be letters;
    13 is always 'Z'. Correcting O->0 in a digit slot is deterministic, not
    a guess. Letter-to-letter confusions (I vs J) cannot be fixed this way
    and are left for the registry lookup to catch.
    """
    cleaned = re.sub(r"[^0-9A-Za-z]", "", raw or "").upper()
    if len(cleaned) != 15:
        return cleaned
    out = []
    for i, char in enumerate(cleaned):
        if i in (0, 1) or 7 <= i <= 10:
            out.append(_TO_DIGIT.get(char, char))
        elif 2 <= i <= 6 or i == 11:
            out.append(_TO_LETTER.get(char, char))
        elif i == 13:
            out.append("Z")
        else:
            out.append(char)
    return "".join(out)


def resolve_gstin(candidate: str) -> tuple[str, bool]:
    """Return (best_gstin, was_repaired).

    If the candidate is not in the registry, try every single-character
    substitution and accept one ONLY if exactly one variant both passes the
    checksum and exists in the registry. One unambiguous match is a corrected
    scan; two would be a guess, so we refuse.
    """
    store = get_store()
    if not candidate:
        return "", False
    if store.lookup_gstin(candidate):
        return candidate, False
    if len(candidate) != 15:
        return candidate, False

    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    matches = set()
    for i in range(15):
        for char in alphabet:
            variant = candidate[:i] + char + candidate[i + 1:]
            if variant != candidate and gstin_checksum_ok(variant) \
                    and store.lookup_gstin(variant):
                matches.add(variant)
                if len(matches) > 1:
                    return candidate, False
    if len(matches) == 1:
        return matches.pop(), True
    return candidate, False


def parse_fields(doc_type: str, text: str) -> dict:
    if doc_type == "gst_certificate":
        raw = re.search(r"GSTIN\s*([0-9A-Za-z]{8,20})", text)
        gstin = repair_gstin(raw.group(1)) if raw else ""
        gstin, repaired = resolve_gstin(gstin)
        return {
            "gstin": gstin,
            "gstin_repaired": repaired,
            "legal_name": _label(text, "Legal name"),
            "trade_name": _label(text, "Trade name"),
            "constitution": _label(text, "Constitution"),
            "date_of_liability": _label(text, "Date of liability"),
        }

    if doc_type == "udyam_certificate":
        number = re.sub(r"\s", "", _label(text, r"Udyam reg\. number"))
        # When the field is blank the label grabs the next line, which is the
        # following label. Treat that as missing rather than as a value.
        if number and not number.upper().startswith("UDYAM"):
            number = ""
        return {
            "udyam_number": number.upper(),
            "enterprise_name": _label(text, "Enterprise name"),
            "major_activity": _label(text, "Major activity"),
            "city": _label(text, "City"),
            "state": _label(text, "State"),
        }

    if doc_type == "address_proof":
        return {
            "billed_to": _label(text, "Billed to"),
            "supply_address": _label(text, "Supply address"),
            "bill_date": _label(text, "Bill date"),
            "consumer_no": _label(text, r"Consumer no\."),
        }

    if doc_type == "signatory_id":
        return {
            "name": _label(text, "Name"),
            "designation": _label(text, "Designation"),
            "for_business": _label(text, "For business"),
            "valid_till": _label(text, "Valid till"),
        }
    return {}


# ======================================================================
# Findings
# ======================================================================
BLOCK, REVIEW, INFO = "block", "review", "info"


@dataclass
class Finding:
    code: str
    severity: str
    message: str
    action: str = ""
    document: str = ""

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity,
                "message": self.message, "action": self.action,
                "document": self.document}


def _normalise_name(name: str) -> str:
    """Strip suffixes and the '(Prop. X)' wrapper so names compare fairly."""
    text = (name or "").lower()
    text = re.sub(r"\(prop\.?\s*", " ", text)
    text = re.sub(r"\b(llp|pvt|private|limited|ltd|co|company|and|&)\b", " ", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def names_match(a: str, b: str,
                threshold: int = NAME_MATCH_THRESHOLD) -> tuple[bool, int, list[str]]:
    """Compare two business names.

    Returns (matches, score, differing_words).

    token_sort_ratio is used rather than token_set_ratio: the latter ignores
    tokens that appear on only one side, so swapping one word of a business
    name — "Shri Physio Clinic" for "Neelgiri Physio Clinic" — still scores 92
    and sails past. Alongside the score we look for any substantial word with
    no counterpart on the other side, which is what a substituted name really
    looks like, and which lets us tell the owner exactly which word is wrong.
    """
    na, nb = _normalise_name(a), _normalise_name(b)
    if not na or not nb:
        return True, 100, []      # nothing to compare: do not invent a failure

    score = int(fuzz.token_sort_ratio(na, nb))
    tokens_a, tokens_b = set(na.split()), set(nb.split())

    def orphans(left: set[str], right: set[str]) -> list[str]:
        return [t for t in left
                if len(t) >= 4
                and max((fuzz.ratio(t, u) for u in right), default=0) < 80]

    differing = sorted(set(orphans(tokens_a, tokens_b)) | set(orphans(tokens_b, tokens_a)))
    return (score >= threshold and not differing), score, differing


def _parse_date(value: str) -> Optional[date]:
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", value or "")
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


# ======================================================================
# The validation chain
# ======================================================================
def validate_application(docs: dict[str, DocumentRead]) -> dict:
    """Run every check across a complete set of documents."""
    store = get_store()
    findings: list[Finding] = []
    extracted: dict = {}
    autofill: dict = {}

    # ---- 1. completeness --------------------------------------------
    for doc_type, label in REQUIRED_DOCUMENTS.items():
        if doc_type not in docs:
            findings.append(Finding(
                "DOC_MISSING", BLOCK,
                f"{_sentence_case(label)} has not been uploaded.",
                f"Upload your {label} to continue.",
                doc_type))

    # ---- 2. image quality -------------------------------------------
    for doc_type, doc in docs.items():
        label = REQUIRED_DOCUMENTS.get(doc_type, doc_type)
        if not doc.legible:
            findings.append(Finding(
                "IMAGE_UNREADABLE", BLOCK,
                f"The {label} is too blurred to read "
                f"(sharpness {doc.blur:.0f}, needs {BLUR_THRESHOLD:.0f}+).",
                "Retake the photo in good light, holding the phone steady and "
                "flat above the page.",
                doc_type))
        elif doc.deskewed:
            findings.append(Finding(
                "IMAGE_STRAIGHTENED", INFO,
                f"The {label} was scanned at {abs(doc.skew):.0f}° and "
                f"was straightened automatically before reading.",
                "", doc_type))

    gst = docs.get("gst_certificate")
    udyam = docs.get("udyam_certificate")
    address = docs.get("address_proof")
    signatory = docs.get("signatory_id")

    registry_gst = None

    # ---- 3. GSTIN ----------------------------------------------------
    if gst and gst.legible:
        gstin = gst.fields.get("gstin", "")
        extracted["gstin"] = mask_gstin(gstin)

        if not gstin:
            findings.append(Finding(
                "GSTIN_UNREADABLE", BLOCK,
                "No GST number could be read from the certificate.",
                "Upload a clearer photo of the certificate.",
                "gst_certificate"))
        else:
            fmt = validate_gstin_format(gstin)
            if not fmt["ok"]:
                findings.append(Finding(
                    fmt["code"], REVIEW,
                    f"The GST number on the certificate ({mask_gstin(gstin)}) "
                    f"is not valid. {fmt['message']}",
                    "Check the number against your certificate. If it is correct "
                    "as printed, a partner will verify it with you.",
                    "gst_certificate"))
            else:
                if gst.fields.get("gstin_repaired"):
                    findings.append(Finding(
                        "GSTIN_OCR_CORRECTED", INFO,
                        "One character of the GST number was misread by OCR and "
                        "was corrected against the GST registry.",
                        "", "gst_certificate"))
                registry_gst = store.lookup_gstin(gstin)
                if registry_gst is None:
                    findings.append(Finding(
                        "GSTIN_NOT_FOUND", REVIEW,
                        f"GST number {mask_gstin(gstin)} is correctly formed but "
                        f"does not exist in the GST registry.",
                        "A partner will verify your registration with you.",
                        "gst_certificate"))
                elif registry_gst["status"] != "Active":
                    status = registry_gst["status"].lower()
                    findings.append(Finding(
                        f"GSTIN_{registry_gst['status'].upper()}", REVIEW,
                        f"This GST registration is {status} in the GST registry.",
                        "Products that need an active GST registration cannot be "
                        "activated. Everything else can go ahead now, and we can "
                        "add the rest once your registration is restored.",
                        "gst_certificate"))
                else:
                    autofill = {
                        "legal_name": registry_gst["legal_name"],
                        "trade_name": registry_gst["trade_name"],
                        "constitution": registry_gst["constitution"],
                        "address": registry_gst["principal_place_of_business"],
                        "state": registry_gst["state"],
                    }

                # name on the document vs name in the registry
                if registry_gst:
                    doc_name = gst.fields.get("legal_name", "")
                    ok, score, differing = names_match(doc_name,
                                                       registry_gst["legal_name"])
                    if not ok:
                        detail = ""
                        if differing:
                            words = ", ".join(f"“{w}”" for w in differing)
                            detail = f" The difference is in {words}."
                        findings.append(Finding(
                            "NAME_MISMATCH_REGISTRY", REVIEW,
                            f"The name on the certificate reads “{doc_name}”, but "
                            f"the GST registry holds “{registry_gst['legal_name']}” "
                            f"for this number (match {score}%).{detail}",
                            "Upload the certificate issued for this GST number, or "
                            "a partner will confirm the details with you.",
                            "gst_certificate"))

    # ---- 4. Udyam -----------------------------------------------------
    if udyam and udyam.legible:
        number = udyam.fields.get("udyam_number", "")
        extracted["udyam_number"] = mask_udyam(number)
        fmt = validate_udyam_format(number)
        if fmt["code"] == "UDYAM_MISSING":
            findings.append(Finding(
                "UDYAM_NUMBER_BLANK", BLOCK,
                "The Udyam certificate does not show a registration number.",
                "Download a fresh copy from udyamregistration.gov.in — the number "
                "is printed at the top.",
                "udyam_certificate"))
        elif not fmt["ok"]:
            findings.append(Finding(
                fmt["code"], BLOCK,
                f"The Udyam number could not be read correctly. {fmt['message']}",
                "Upload a clearer copy of the certificate.",
                "udyam_certificate"))
        else:
            record = store.lookup_udyam(number)
            if record is None:
                findings.append(Finding(
                    "UDYAM_NOT_FOUND", REVIEW,
                    f"Udyam number {mask_udyam(number)} is not in the Udyam registry.",
                    "Check the number on your certificate; a partner can verify it.",
                    "udyam_certificate"))
            else:
                autofill.setdefault("enterprise_type", record["enterprise_type"])
                autofill.setdefault("major_activity", record["major_activity"])

    # ---- 5. cross-document name consistency ---------------------------
    reference = None
    if registry_gst:
        reference = registry_gst["trade_name"]
    elif gst and gst.legible:
        reference = gst.fields.get("trade_name")

    if reference:
        for doc, key, label in (
            (udyam, "enterprise_name", "Udyam certificate"),
            (address, "billed_to", "address proof"),
            (signatory, "for_business", "signatory ID"),
        ):
            if not doc or not doc.legible:
                continue
            value = doc.fields.get(key, "")
            if not value:
                continue
            ok, score, differing = names_match(value, reference)
            if not ok:
                detail = ""
                if differing:
                    words = ", ".join(f"“{w}”" for w in differing)
                    detail = f" The difference is in {words}."
                findings.append(Finding(
                    "NAME_MISMATCH_DOCUMENTS", REVIEW,
                    f"The business name on the {label} reads “{value}”, which does "
                    f"not match “{reference}” (match {score}%).{detail}",
                    "All documents must be in the same business name.",
                    doc.doc_type))

    # ---- 6. address proof freshness -----------------------------------
    if address and address.legible:
        bill_date = _parse_date(address.fields.get("bill_date", ""))
        extracted["address_proof_date"] = address.fields.get("bill_date", "")
        if bill_date is None:
            findings.append(Finding(
                "ADDRESS_DATE_UNREADABLE", BLOCK,
                "The date on the address proof could not be read.",
                "Upload a clearer copy showing the bill date.",
                "address_proof"))
        else:
            age = (TODAY - bill_date).days
            if age > ADDRESS_PROOF_MAX_AGE_DAYS:
                cutoff = date.fromordinal(TODAY.toordinal() - ADDRESS_PROOF_MAX_AGE_DAYS)
                findings.append(Finding(
                    "ADDRESS_PROOF_STALE", BLOCK,
                    f"The address proof is dated {bill_date.isoformat()}, which is "
                    f"{age} days old. We can only accept a bill from the last "
                    f"{ADDRESS_PROOF_MAX_AGE_DAYS} days.",
                    f"Upload a utility bill dated after {cutoff.isoformat()}.",
                    "address_proof"))
            elif age < 0:
                findings.append(Finding(
                    "ADDRESS_PROOF_FUTURE", REVIEW,
                    f"The address proof is dated {bill_date.isoformat()}, which is "
                    f"in the future.",
                    "A partner will check this document with you.",
                    "address_proof"))

    # ---- 7. state consistency ------------------------------------------
    if registry_gst and udyam and udyam.legible:
        number = udyam.fields.get("udyam_number", "")
        match = re.match(r"UDYAM-([A-Z]{2})-", number)
        if match:
            udyam_state = UDYAM_STATE_CODES.get(match.group(1))
            if udyam_state and udyam_state != registry_gst["state"]:
                findings.append(Finding(
                    "STATE_MISMATCH", REVIEW,
                    f"The GST registration is in {registry_gst['state']} but the "
                    f"Udyam registration is in {udyam_state}.",
                    "A partner will confirm which address this connection is for.",
                    "udyam_certificate"))

    # ---- 8. signatory ---------------------------------------------------
    if signatory and signatory.legible:
        extracted["signatory"] = signatory.fields.get("name", "")
        extracted["signatory_role"] = signatory.fields.get("designation", "")
        expiry = _parse_date(signatory.fields.get("valid_till", ""))
        if expiry and expiry < TODAY:
            findings.append(Finding(
                "SIGNATORY_ID_EXPIRED", BLOCK,
                f"The signatory ID expired on {expiry.isoformat()}.",
                "Upload a current ID for the authorised signatory.",
                "signatory_id"))

    # ---- verdict ---------------------------------------------------------
    severities = {f.severity for f in findings}
    if REVIEW in severities:
        status, headline = "MANUAL_REVIEW", "Needs a quick check by our team"
    elif BLOCK in severities:
        status, headline = "FIX_AND_RESUBMIT", "Almost there — a document needs fixing"
    else:
        status, headline = "AUTO_APPROVED", "Verified — all documents check out"

    order = {BLOCK: 0, REVIEW: 1, INFO: 2}
    findings.sort(key=lambda f: order[f.severity])

    return {
        "status": status,
        "headline": headline,
        "findings": [f.to_dict() for f in findings],
        "blocking_count": sum(1 for f in findings if f.severity in (BLOCK, REVIEW)),
        "extracted": extracted,
        "autofill": autofill,
        "documents": {
            name: {"blur": round(doc.blur, 1), "skew": round(doc.skew, 2),
                   "deskewed": doc.deskewed, "legible": doc.legible,
                   "fields": {k: v for k, v in doc.fields.items()
                              if k not in ("gstin",)}}
            for name, doc in docs.items()
        },
    }


def verify_specimen(applicant: str, kyc_dir: Optional[Path] = None) -> dict:
    """Run the full pipeline over one of the K01-K12 specimen applicants."""
    from .config import KYC_DIR
    base = Path(kyc_dir or KYC_DIR)
    docs: dict[str, DocumentRead] = {}
    for doc_type in REQUIRED_DOCUMENTS:
        path = base / f"{applicant}_{doc_type}.png"
        if path.exists():
            docs[doc_type] = read_document(path, doc_type)
    result = validate_application(docs)
    result["applicant"] = applicant
    return result
