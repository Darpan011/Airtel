"""Central configuration for Project Udyam Digital."""
import os
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
KYC_DIR = DATA_DIR / "kyc_documents"
STATIC_DIR = BASE_DIR / "static"

# The dataset README fixes "today" at 2026-07-15 for all date-based checks.
TODAY = date(2026, 7, 15)

# Address proof must be no older than this many days (README rule).
ADDRESS_PROOF_MAX_AGE_DAYS = 90

# Fuzzy name-match threshold (0-100) for cross-document name consistency.
NAME_MATCH_THRESHOLD = 85

# Gemini (free tier). Set GEMINI_API_KEY in the environment or a .env file.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# Segment codes used by product_catalogue.target_segments
SEGMENT_CODES = {
    "Micro retail & services": "micro",
    "Professional offices": "prof",
    "Small multi-site businesses": "multi",
    "Small manufacturers": "mfg",
}
SEGMENT_LABELS = {v: k for k, v in SEGMENT_CODES.items()}

# GSTIN state code -> state name (only the codes present in the synthetic data
# plus the common ones, used for cross-document state consistency checks).
GST_STATE_CODES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
    "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana", "07": "Delhi",
    "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam",
    "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
    "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "27": "Maharashtra", "29": "Karnataka", "30": "Goa", "32": "Kerala",
    "33": "Tamil Nadu", "34": "Puducherry", "36": "Telangana",
    "37": "Andhra Pradesh",
}

# Udyam state abbreviation -> state name
UDYAM_STATE_CODES = {
    "AP": "Andhra Pradesh", "AS": "Assam", "BR": "Bihar", "CG": "Chhattisgarh",
    "DL": "Delhi", "GJ": "Gujarat", "HR": "Haryana", "HP": "Himachal Pradesh",
    "JH": "Jharkhand", "KA": "Karnataka", "KL": "Kerala", "MP": "Madhya Pradesh",
    "MH": "Maharashtra", "OD": "Odisha", "PB": "Punjab", "RJ": "Rajasthan",
    "TN": "Tamil Nadu", "TS": "Telangana", "UP": "Uttar Pradesh",
    "UK": "Uttarakhand", "WB": "West Bengal", "GA": "Goa",
}
