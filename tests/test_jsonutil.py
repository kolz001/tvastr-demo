from tvastr.analysis._jsonutil import extract_all_json, extract_json


def test_extract_all_json_multiple_objects():
    text = '{"a": 1}\nsome prose\n{"b": 2, "c": 3}'
    assert extract_all_json(text) == [{"a": 1}, {"b": 2, "c": 3}]


def test_extract_all_json_single():
    assert extract_all_json('prefix {"x": 1} suffix') == [{"x": 1}]


def test_extract_all_json_none():
    assert extract_all_json("no json here") == []


def test_extract_json_still_returns_first():
    # extract_json (singular) keeps its first-object contract.
    assert extract_json('{"a": 1}{"b": 2}') == {"a": 1}
