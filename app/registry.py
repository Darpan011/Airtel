"""Mock GSTIN and Udyam lookup services.

In production these would be the GSTN public API and the Udyam portal. Here
they are backed by the synthetic registries, but the *contract* is identical:
give an identifier, get back the authoritative record or a clear failure.

Every response carries a `verified` flag and a human-readable `message` so the
UI never has to guess why something was rejected.
"""
from .data import get_store
from .validators import (
    mask_gstin,
    mask_udyam,
    validate_gstin_format,
    validate_udyam_format,
)


def gstin_lookup(gstin: str) -> dict:
    """Validate a GSTIN structurally, then resolve it against the registry."""
    fmt = validate_gstin_format(gstin)
    if not fmt["ok"]:
        return {
            "verified": False,
            "stage": "format",
            "code": fmt["code"],
            "message": fmt["message"],
            "input": mask_gstin(gstin),
        }

    clean = fmt["value"]
    record = get_store().lookup_gstin(clean)
    if record is None:
        return {
            "verified": False,
            "stage": "registry",
            "code": "GSTIN_NOT_FOUND",
            "message": "This GSTIN is well-formed but does not exist in the GST registry.",
            "input": mask_gstin(clean),
        }

    status = record["status"]
    if status != "Active":
        return {
            "verified": False,
            "stage": "status",
            "code": f"GSTIN_{status.upper()}",
            "message": (
                f"This GSTIN is {status.lower()} in the GST registry. "
                "Products that require an active GST registration cannot be sold on it."
            ),
            "input": mask_gstin(clean),
            "status": status,
            "record": _public_gstin(record),
        }

    return {
        "verified": True,
        "stage": "complete",
        "code": "GSTIN_ACTIVE",
        "message": f"Verified — active GST registration for {record['legal_name']}.",
        "input": mask_gstin(clean),
        "status": status,
        "record": _public_gstin(record),
    }


def udyam_lookup(number: str) -> dict:
    """Validate a Udyam number structurally, then resolve it."""
    fmt = validate_udyam_format(number)
    if not fmt["ok"]:
        return {
            "verified": False,
            "stage": "format",
            "code": fmt["code"],
            "message": fmt["message"],
            "input": mask_udyam(number),
        }

    clean = fmt["value"]
    record = get_store().lookup_udyam(clean)
    if record is None:
        return {
            "verified": False,
            "stage": "registry",
            "code": "UDYAM_NOT_FOUND",
            "message": "This Udyam number is well-formed but is not in the Udyam registry.",
            "input": mask_udyam(clean),
        }

    return {
        "verified": True,
        "stage": "complete",
        "code": "UDYAM_FOUND",
        "message": f"Verified — {record['enterprise_type']} enterprise, {record['enterprise_name']}.",
        "input": mask_udyam(clean),
        "record": _public_udyam(record),
    }


# ----------------------------------------------------------------------
# Response shaping — we return only the fields the journey actually needs.
# Purpose limitation (DPDP s.6) applied at the API boundary, not just in the UI.
# ----------------------------------------------------------------------
def _public_gstin(record: dict) -> dict:
    return {
        "legal_name": record["legal_name"],
        "trade_name": record["trade_name"],
        "constitution": record["constitution"],
        "principal_place_of_business": record["principal_place_of_business"],
        "state": record["state"],
        "registration_date": record["registration_date"],
        "status": record["status"],
    }


def _public_udyam(record: dict) -> dict:
    return {
        "enterprise_name": record["enterprise_name"],
        "enterprise_type": record["enterprise_type"],
        "major_activity": record["major_activity"],
        "nic_description": record["nic_description"],
        "state": record["state"],
        "city": record["city"],
        "date_of_registration": record["date_of_registration"],
    }
