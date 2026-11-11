"""Normalisation and validation of identifiers, names, titles and dates.

Every value is normalised before it is compared with Wikidata, so that
"https://doi.org/10.1/abc" and "10.1/ABC" are recognised as the same DOI and
no duplicate statement is added.
"""
from __future__ import annotations

import calendar
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Optional, Tuple

# --------------------------------------------------------------------------- ORCID
ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")


def orcid_checksum_ok(orcid: str) -> bool:
    """ISO 7064 11,2 check digit used by ORCID."""
    digits = orcid.replace("-", "")
    total = 0
    for ch in digits[:-1]:
        total = (total + int(ch)) * 2
    remainder = (12 - total % 11) % 11
    expected = "X" if remainder == 10 else str(remainder)
    return digits[-1] == expected


def normalize_orcid(value: Optional[str]) -> Optional[str]:
    """Return a bare, upper-case, checksum-valid ORCID iD, or None."""
    if not value or not isinstance(value, str):
        return None
    v = re.sub(r"^https?://(www\.)?orcid\.org/", "", value.strip(), flags=re.I).upper()
    if not ORCID_RE.match(v) or not orcid_checksum_ok(v):
        return None
    return v


# --------------------------------------------------------------------------- DOI
DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")


def normalize_doi(value: Optional[str]) -> Optional[str]:
    """Return a bare, upper-case DOI (Wikidata P356 convention), or None."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    v = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", v, flags=re.I)
    v = re.sub(r"^doi:\s*", "", v, flags=re.I)
    v = v.upper()
    return v if DOI_RE.match(v) else None


# --------------------------------------------------------------------------- Handle
HANDLE_RE = re.compile(r"^\d+(\.\d+)*/\S+$")


def normalize_handle(value: Optional[str]) -> Optional[str]:
    """Return a bare handle like '11250/3093139' (Wikidata P1184 format), or None."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    v = re.sub(r"^https?://hdl\.handle\.net/", "", v, flags=re.I)
    v = re.sub(r"^hdl:\s*", "", v, flags=re.I)
    v = v.split("?")[0].rstrip("/")
    return v if HANDLE_RE.match(v) else None


def handle_key(value: Optional[str]) -> Optional[str]:
    """Comparison key: handles are case-insensitive for ASCII characters."""
    v = normalize_handle(value)
    return v.lower() if v else None


# --------------------------------------------------------------------------- Text
_TRANSLATE = str.maketrans({
    "ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "å": "a", "Å": "a",
    "ß": "ss", "đ": "d", "Đ": "d", "ł": "l", "Ł": "l",
})


def fold(text: str) -> str:
    """Lower-case, strip accents and punctuation; keeps letters of any script."""
    text = (text or "").translate(_TRANSLATE)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\- ]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def split_name(name: str) -> Tuple[str, str]:
    """Return (surname key, first initial) for common author-name forms.

    Handles "Ida Kristin Bos-Haugen", "Bos-Haugen, Ida K.", "I. K. Bos-Haugen"
    and Vancouver style "Bos-Haugen IK".
    """
    if not name:
        return "", ""
    if "," in name:
        last, first = name.split(",", 1)
        first_tokens = fold(first).split()
        return fold(last).replace("-", "").replace(" ", ""), (first_tokens[0][0] if first_tokens else "")
    tokens = fold(name).split()
    if not tokens:
        return "", ""
    if len(tokens) == 1:
        return tokens[0].replace("-", ""), ""
    if len(tokens[-1]) <= 2 and len(tokens[0]) > 2:  # surname first, trailing initials
        return tokens[0].replace("-", ""), tokens[-1][0]
    return tokens[-1].replace("-", ""), tokens[0][0]


def names_match(a: str, b: str) -> bool:
    """Conservative match: identical surname and compatible first initial."""
    la, fa = split_name(a)
    lb, fb = split_name(b)
    if not la or la != lb:
        return False
    return not fa or not fb or fa == fb


def title_key(title: str) -> str:
    return re.sub(r"\s+", " ", fold(title).replace("-", " ").replace("_", " ")).strip()


def title_similarity(a: str, b: str) -> Optional[float]:
    """0..1 similarity of two titles; None when either is empty."""
    ka, kb = title_key(a), title_key(b)
    if not ka or not kb:
        return None
    if ka == kb:
        return 1.0
    shorter = min(len(ka), len(kb))
    if shorter >= 20 and (ka.startswith(kb) or kb.startswith(ka)):
        return 1.0  # one side lacks the subtitle
    return SequenceMatcher(None, ka, kb).ratio()


def clean_title(title: Optional[str]) -> Optional[str]:
    if not title or not isinstance(title, str):
        return None
    t = re.sub(r"\s+", " ", title).strip()
    if not t or len(t) > 1500:  # Wikidata monolingual text limit
        return None
    return t


# --------------------------------------------------------------------------- Language
LEXVO_TO_WIKIDATA = {
    "eng": "en", "nob": "nb", "nno": "nn", "nor": "no", "swe": "sv", "dan": "da",
    "deu": "de", "ger": "de", "fra": "fr", "fre": "fr", "spa": "es", "ita": "it",
    "fin": "fi", "isl": "is", "ice": "is", "nld": "nl", "dut": "nl", "por": "pt",
    "rus": "ru", "pol": "pl", "sme": "se", "smj": "smj", "sma": "sma", "lat": "la",
    "zho": "zh", "chi": "zh", "jpn": "ja", "ara": "ar",
}


def lexvo_to_wikidata(uri: Optional[str]) -> Optional[str]:
    """'http://lexvo.org/id/iso639-3/eng' -> 'en' (None when unknown)."""
    if not uri or not isinstance(uri, str):
        return None
    code = uri.rstrip("/").rsplit("/", 1)[-1].lower()
    return LEXVO_TO_WIKIDATA.get(code)


# --------------------------------------------------------------------------- Dates
def _as_int(value) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def nva_date(pub_date, max_year: int) -> Optional[Tuple[str, int]]:
    """NVA PublicationDate -> (Wikidata time string, precision) or None."""
    if not isinstance(pub_date, dict):
        return None
    y = _as_int(pub_date.get("year"))
    if y is None or not 1000 <= y <= max_year:
        return None
    m = _as_int(pub_date.get("month"))
    d = _as_int(pub_date.get("day"))
    if m and 1 <= m <= 12:
        if d and 1 <= d <= calendar.monthrange(y, m)[1]:
            return f"+{y:04d}-{m:02d}-{d:02d}T00:00:00Z", 11
        return f"+{y:04d}-{m:02d}-00T00:00:00Z", 10
    return f"+{y:04d}-00-00T00:00:00Z", 9


def wikidata_year(time_value) -> Optional[int]:
    if not isinstance(time_value, dict):
        return None
    m = re.match(r"^([+-]\d+)-", time_value.get("time", ""))
    return int(m.group(1)) if m else None
