
import re
import unicodedata

import pandas as pd
from rapidfuzz import fuzz




def rm_accents(x: str) -> str:
    """Strip accented characters to ASCII equivalents."""
    return unicodedata.normalize("NFKD", x).encode("ASCII", "ignore").decode("ASCII")


def collapse_ws(s: str) -> str:
    """Collapse multiple whitespace characters into a single space."""
    return re.sub(r"\s+", " ", s).strip()




PUNCT_NAME_DROP = r"[^A-Z'\-\s]"
PUNCT_ADDR_KEEP = r"[^A-Z0-9'\-\s]"
TITLE_RE = r"\b(MR|MRS|MS|MISS|DR|PROF|REV|SRGT|SGT|OFFICER|ATTY|ATTORNEY|JUDGE)\b"

DIR_MAP = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW",
    "SOUTHEAST": "SE", "SOUTHWEST": "SW",
}

STREET_MAP = {
    "STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "LANE": "LN",
    "DRIVE": "DR", "BOULEVARD": "BLVD", "COURT": "CT", "PLACE": "PL",
    "PARKWAY": "PKWY", "CIRCLE": "CIR", "HIGHWAY": "HWY",
}

# Pre-compile the street-type pattern for performance
_STREET_PAT = re.compile(
    r"\b(" + "|".join(map(re.escape, STREET_MAP.keys())) + r")\b"
)




def clean_name(val):
    """Normalise a name field: uppercase, strip titles/punctuation, collapse whitespace."""
    if val is None:
        return None
    v = str(val).strip()
    if v == "":
        return None
    v = rm_accents(v).upper()
    v = re.sub(TITLE_RE, "", v)
    v = re.sub(PUNCT_NAME_DROP, " ", v)
    v = collapse_ws(v)
    return v or None


def clean_state(val):
    """Extract a two-letter state code."""
    if val is None:
        return None
    v = rm_accents(str(val)).upper()
    v = re.sub(r"[^A-Z]", "", v)[:2]
    return v or None


def clean_zip(val):
    """Extract the first five digits of a ZIP code."""
    if val is None:
        return None
    digits = re.sub(r"\D", "", str(val))[:5]
    return digits if digits else None


def clean_street(*parts):
    """Clean and standardise one or more street-address fields.

    Handles: PO BOX normalisation, APT/STE/# variations,
    directional abbreviations, street-type abbreviations.
    """
    v = " ".join(
        str(p).strip()
        for p in parts
        if p is not None and str(p).strip() != ""
    )
    if v.strip() == "":
        return None
    v = rm_accents(v).upper()
    v = re.sub(r"\bP\.?\s*O\.?\s*B(?:OX)?\b", "PO BOX", v)
    v = re.sub(r"\b(APARTMENT|APT)\b", "APT", v)
    v = re.sub(r"\b(SUITE|STE)\b", "STE", v)
    v = re.sub(r"#\s*([A-Z0-9\-]+)", r"APT \1", v)
    v = re.sub(r"[.,]", "", v)
    v = re.sub(
        r"\b(NORTHWEST|NORTHEAST|SOUTHWEST|SOUTHEAST|NORTH|SOUTH|EAST|WEST)\b",
        lambda m: DIR_MAP[m.group(0)],
        v,
    )
    v = _STREET_PAT.sub(lambda m: STREET_MAP[m.group(0)], v)
    v = re.sub(PUNCT_ADDR_KEEP, " ", v)
    v = collapse_ws(v)
    return v or None



# DataFrame-level helpers

def build_full_name(df):
    """Create full_name_clean from first/middle/last_name_clean columns."""
    df["full_name_clean"] = (
        df["first_name_clean"].fillna("")
        + " "
        + df["middle_name_clean"].fillna("")
        + " "
        + df["last_name_clean"].fillna("")
    ).map(lambda x: collapse_ws(x) if x.strip() != "" else None)
    return df


def clean_sok_names(df):
    """Apply name cleaning to SOK-schema columns."""
    df["first_name_clean"]  = df["First_Name"].map(clean_name)
    df["middle_name_clean"] = df["Middle_Name"].map(clean_name)
    df["last_name_clean"]   = df["Last_Name"].map(clean_name)
    return build_full_name(df)


def clean_sok_address(df):
    """Apply address cleaning to SOK-schema columns."""
    df["street_clean"] = df.apply(
        lambda r: clean_street(
            r["Residential_Address_Street"],
            r["Residential_Address_Street_2"],
        ),
        axis=1,
    )
    df["city_clean"]  = df["Residential_Address_City"].map(clean_name)
    df["state_clean"] = df["Residential_Address_State"].map(clean_state)
    df["zip_clean"]   = df["Residential_Address_Zip"].map(clean_zip)
    return df


def clean_kelmar_names(df):
    """Apply name cleaning to Kelmar-schema columns."""
    df["first_name_clean"]  = df["NameFirst"].map(clean_name)
    df["middle_name_clean"] = df["NameMiddle"].map(clean_name)
    df["last_name_clean"]   = df["NameLast"].map(clean_name)
    return build_full_name(df)


def clean_kelmar_address(df):
    """Apply address cleaning to Kelmar-schema columns (Address1/2/3)."""
    df["street_clean"] = df.apply(
        lambda r: clean_street(r["Address1"], r["Address2"], r["Address3"]),
        axis=1,
    )
    df["city_clean"]  = df["City"].map(clean_name)
    df["state_clean"] = df["State"].map(clean_state)
    df["zip_clean"]   = df["Zip"].map(clean_zip)
    return df



# Similarity scoring

def similarity(a, b):
    """Weighted ratio similarity (0–100). Returns 0 if either value is null."""
    if pd.isna(a) or pd.isna(b):
        return 0
    return fuzz.WRatio(a, b)