from nvabot.normalize import (handle_key, lexvo_to_wikidata, names_match, normalize_doi, normalize_handle,
                              normalize_orcid, nva_date, title_similarity)
from nvabot.nva import institution_number, person_id_from_uri, record_handles

from .fixtures import RECORD


def test_orcid():
    assert normalize_orcid("https://orcid.org/0000-0002-1825-0097") == "0000-0002-1825-0097"
    assert normalize_orcid("0000-0002-1694-233x") == "0000-0002-1694-233X"
    assert normalize_orcid("0000-0002-1825-0098") is None  # bad checksum
    assert normalize_orcid("not an orcid") is None


def test_doi():
    assert normalize_doi("https://doi.org/10.1371/journal.pone.0047855") == "10.1371/JOURNAL.PONE.0047855"
    assert normalize_doi("doi:10.1000/xyz") == "10.1000/XYZ"
    assert normalize_doi("https://example.org/10.1/x") is None


def test_handle():
    assert normalize_handle("https://hdl.handle.net/11250/3093139") == "11250/3093139"
    assert normalize_handle("hdl:11250/3093139/") == "11250/3093139"
    assert normalize_handle("nonsense") is None
    assert handle_key("11250/ABC") == handle_key("https://hdl.handle.net/11250/abc")
    assert record_handles(RECORD) == ["11250/3093139", "11250/2222222"]


def test_names():
    assert names_match("Ida Kristin Bos-Haugen", "I. K. Bos-Haugen")
    assert names_match("Ida Kristin Bos-Haugen", "Bos-Haugen, Ida")
    assert names_match("Ida Kristin Bos-Haugen", "Bos-Haugen IK")
    assert names_match("Øyvind Ås", "Oyvind As")
    assert not names_match("Ida Bos-Haugen", "Per Bos-Haugen")
    assert not names_match("Ida Bos-Haugen", "Ida Haugen")


def test_names_word_by_word_never_fuzzy():
    # the reviewer's examples (Wikidata:Requests for permissions/Bot/NVAImportBot)
    assert names_match("A. Nobel", "Alfred Nobel")
    assert names_match("A. B. Nobel", "Alfred Nobel")
    assert not names_match("Olfred Nabel", "Alfred Nobel")          # small edit distance, still no match
    # every given name is compared, not only the first initial
    assert not names_match("Anna Nobel", "Alfred Nobel")
    assert not names_match("A. C. Nobel", "Alfred Bernhard Nobel")
    assert names_match("A. B. Nobel", "Alfred Bernhard Nobel")
    assert not names_match("Kristin Bos-Haugen", "Ida Kristin Bos-Haugen")
    # a bare surname is never enough
    assert not names_match("Nobel", "Alfred Nobel")
    # name forms
    assert names_match("Dupont JP", "Jean-Pierre Dupont")
    assert names_match("J.-P. Dupont", "Jean-Pierre Dupont")
    assert not names_match("Dupont JM", "Jean-Pierre Dupont")
    assert names_match("IDA BOS-HAUGEN", "Ida Bos-Haugen")          # all caps is not read as initials


def test_titles_and_dates():
    assert title_similarity("A Simple Model: part 1", "a simple model - part 1") == 1.0
    assert title_similarity("Something else entirely", "A simple model for avalanches") < 0.5
    assert title_similarity("", "x") is None
    assert nva_date({"year": "2023", "month": "08", "day": "24"}, 2027) == ("+2023-08-24T00:00:00Z", 11)
    assert nva_date({"year": "2023", "month": "2", "day": "30"}, 2027) == ("+2023-02-00T00:00:00Z", 10)
    assert nva_date({"year": "2023"}, 2027) == ("+2023-00-00T00:00:00Z", 9)
    assert nva_date({"year": "2999"}, 2027) is None
    assert lexvo_to_wikidata("http://lexvo.org/id/iso639-3/nob") == "nb"


def test_ids():
    assert person_id_from_uri("https://api.nva.unit.no/cristin/person/15120") == "15120"
    assert person_id_from_uri("https://evil.example/x") is None
    assert institution_number("https://api.nva.unit.no/cristin/organization/185.53.44.12") == "185"


def test_current_lag_parsing():
    from nvabot.wikidata import WikidataReader

    class Resp:
        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    class Session:
        def __init__(self, data):
            self.data = data

        def get(self, *a, **k):
            return Resp(self.data)

    r = WikidataReader.__new__(WikidataReader)
    r.api_url = "https://www.wikidata.org/w/api.php"
    r.session = Session({"error": {"code": "maxlag", "info": "Waiting for all: 12.4 seconds lagged.",
                                   "host": "all", "lag": 12.4, "type": "wikibase-queryservice"}})
    assert r.current_lag() == (12.4, "wikibase-queryservice")
    r.session = Session({"error": {"code": "maxlag", "info": "Waiting for db1: 7 seconds lagged."}})
    assert r.current_lag() == (7.0, "")
    r.session = Session({"batchcomplete": True, "query": {}})
    assert r.current_lag() == (0.0, "")
