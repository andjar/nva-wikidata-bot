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
from typing import List, Optional, Tuple

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


def _is_initials_block(token: str, all_caps: bool) -> bool:
    """'IK', 'I.K.', 'J.-P.', 'A' - upper-case initials, possibly run together (Vancouver style)."""
    letters = token.replace(".", "").replace("-", "")
    if not (1 <= len(letters) <= 3 and letters.isalpha() and letters.isupper()):
        return False
    # In an all-caps name ("IDA BOS-HAUGEN") only dotted or single letters are initials.
    return not all_caps or "." in token or len(letters) == 1


def _surname_key(text: str) -> str:
    return fold(text).replace("-", "").replace(" ", "")


def _given_tokens(text: str, all_caps: bool) -> List[str]:
    """Given names as a list of full names and single-letter initials, in order."""
    out: List[str] = []
    for part in text.split():
        if _is_initials_block(part, all_caps):
            out += [fold(ch) for ch in part if ch.isalpha()]
        else:
            out += [t for t in fold(part).replace("-", " ").split() if t]
    return out


def parse_name(name: str) -> Tuple[str, List[str]]:
    """(surname key, given-name tokens) for common author-name forms.

    "Ida Kristin Bos-Haugen", "Bos-Haugen, Ida K.", "I. K. Bos-Haugen", "Bos-Haugen IK",
    "Jean-Pierre Dupont" / "J.-P. Dupont" / "Dupont JP".
    """
    raw = unicodedata.normalize("NFC", name or "").strip()
    if not raw:
        return "", []
    all_caps = not any(c.islower() for c in raw)
    if "," in raw:
        last, first = raw.split(",", 1)
        return _surname_key(last), _given_tokens(first, all_caps)
    parts = raw.split()
    if len(parts) == 1:
        return _surname_key(parts[0]), []
    # Vancouver style: surname first, initials last ("Bos-Haugen IK")
    if _is_initials_block(parts[-1], all_caps) and not _is_initials_block(parts[0], all_caps):
        return _surname_key(" ".join(parts[:-1])), _given_tokens(parts[-1], all_caps)
    return _surname_key(parts[-1]), _given_tokens(" ".join(parts[:-1]), all_caps)


def names_match(a: str, b: str) -> bool:
    """Word-by-word name comparison, never edit distance.

    The surname must be identical (ignoring case, accents and hyphens). Given names are compared
    in order, word by word: two full names must be equal, an initial must equal the first letter
    of the other name. Extra given names on one side are allowed ("A. Nobel" = "Alfred Bernhard
    Nobel"); a bare surname never matches. So "A. Nobel" and "A. B. Nobel" match "Alfred Nobel",
    while "Anna Nobel", "A. C. Nobel" (vs "Alfred Bernhard Nobel") and "Olfred Nabel" do not.
    """
    la, ga = parse_name(a)
    lb, gb = parse_name(b)
    if not la or la != lb or not ga or not gb:
        return False
    for x, y in zip(ga, gb):
        if len(x) == 1 or len(y) == 1:
            if x[0] != y[0]:
                return False
        elif x != y:
            return False
    return True


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
