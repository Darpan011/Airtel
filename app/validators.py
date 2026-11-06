"""Format validators for GSTIN and Udyam numbers.

These are real structural checks, not mocks. The synthetic data in the pack is
generated with genuine GSTIN checksums, so a correct implementation separates
the valid identifiers from the planted bad ones.
"""
import re
from typing import Optional

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# 2 digit state + 10 char PAN + 1 entity char + 'Z' + 1 checksum char.
# NOTE: the dataset deliberately uses 'X' as the 4th PAN character (an invalid
# real-world entity type) so no identifier can collide with a live taxpayer.
# The README instructs us to accept it, so [A-Z] is correct here.
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{3}[A-Z][A-Z][0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")
UDYAM_RE = re.compile(r"^UDYAM-[A-Z]{2}-[0-9]{2}-[0-9]{7}$")


def gstin_checksum_ok(gstin: str) -> bool:
    """Verify the 15th character using the official GSTIN mod-36 algorithm."""
    if not gstin or len(gstin) != 15:
        return False
    gstin = gstin.upper()
    if any(c not in ALPHABET for c in gstin):
        return False
    total = 0
    for i, char in enumerate(gstin[:14]):
        value = ALPHABET.index(char) * (2 if i % 2 else 1)
        total += value // 36 + value % 36
    expected = ALPHABET[(36 - total % 36) % 36]
    return expected == gstin[14]


def validate_gstin_format(gstin: Optional[str]) -> dict:
    """Return a structured verdict for a GSTIN string."""
    if not gstin:
        return {"ok": False, "code": "GSTIN_MISSING",
                "message": "No GSTIN was provided."}
    gstin = gstin.strip().upper().replace(" ", "")
    if len(gstin) != 15:
        return {"ok": False, "code": "GSTIN_LENGTH",
                "message": f"A GSTIN must be exactly 15 characters; this one has {len(gstin)}."}
    if not GSTIN_RE.match(gstin):
        return {"ok": False, "code": "GSTIN_FORMAT",
                "message": "This does not match the GSTIN structure "
                           "(2-digit state code + 10-character PAN + entity code + Z + checksum)."}
    if not gstin_checksum_ok(gstin):
        return {"ok": False, "code": "GSTIN_CHECKSUM",
                "message": "The GSTIN checksum does not match — the number has been "
                           "mistyped or altered."}
    return {"ok": True, "code": "GSTIN_VALID", "message": "GSTIN format and checksum are valid.",
            "value": gstin}


def validate_udyam_format(number: Optional[str]) -> dict:
    """Return a structured verdict for a Udyam registration number."""
    if not number or not number.strip():
        return {"ok": False, "code": "UDYAM_MISSING",
                "message": "The Udyam registration number is blank or missing."}
    number = number.strip().upper().replace(" ", "")
    if not UDYAM_RE.match(number):
        return {"ok": False, "code": "UDYAM_FORMAT",
                "message": "This does not match the Udyam format "
                           "(UDYAM-<state>-<district>-<7 digits>)."}
    return {"ok": True, "code": "UDYAM_VALID", "message": "Udyam number format is valid.",
            "value": number}


def mask_gstin(gstin: Optional[str]) -> str:
    """Mask a GSTIN for display and logs — DPDP data-minimisation in practice."""
    if not gstin or len(gstin) < 15:
        return "***"
    return f"{gstin[:4]}{'*' * 7}{gstin[11:]}"


def mask_udyam(number: Optional[str]) -> str:
    """Mask a Udyam number for display and logs."""
    if not number or len(number) < 12:
        return "***"
    parts = number.split("-")
    if len(parts) != 4:
        return "***"
    return f"{parts[0]}-{parts[1]}-{parts[2]}-***{parts[3][-3:]}"
