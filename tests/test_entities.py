import pytest

from wactx import entities


def test_spacy_ner_pipeline_loads_and_extracts():
    # enrich crashed on Python 3.14 with spaCy < 3.8.18 (pydantic v1 in confection);
    # nothing exercised the spaCy path before this test.
    pytest.importorskip("en_core_web_sm")
    entities._NLP = None
    nlp = entities._get_nlp()
    assert nlp.has_pipe("ner"), "en_core_web_sm should load with its NER pipe"
    [found] = entities._extract_spacy(
        ["Satya Nadella met Sundar Pichai at Google in London."], ["Someone Else"]
    )
    assert found, "expected at least one entity from a sentence full of named entities"
