"""extract_plug_span — <<PLUG>>...<</PLUG>> marker parsing in sentence_prep.py."""
from scripts.sentence_prep import extract_plug_span


def test_no_marker_returns_text_unchanged():
    text = "First sentence. Second sentence."
    cleaned, span = extract_plug_span(text)
    assert cleaned == text
    assert span is None


def test_single_marker_strips_tokens_and_returns_span():
    text = "Intro. <<PLUG>>Buy my ebook now.<</PLUG>> Outro."
    cleaned, span = extract_plug_span(text)
    assert "<<PLUG>>" not in cleaned
    assert "<</PLUG>>" not in cleaned
    assert cleaned == "Intro. Buy my ebook now. Outro."
    assert span is not None
    start, end = span
    assert cleaned[start:end] == "Buy my ebook now."


def test_marker_is_case_insensitive():
    text = "Intro. <<plug>>Pitch here.<</plug>> Outro."
    cleaned, span = extract_plug_span(text)
    assert cleaned == "Intro. Pitch here. Outro."
    start, end = span
    assert cleaned[start:end] == "Pitch here."


def test_multiline_marker_content():
    text = "Intro.\n<<PLUG>>Line one.\nLine two.<</PLUG>>\nOutro."
    cleaned, span = extract_plug_span(text)
    start, end = span
    assert cleaned[start:end] == "Line one.\nLine two."


def test_multiple_markers_uses_first_and_strips_all(capsys):
    text = "<<PLUG>>First pitch.<</PLUG>> Middle. <<PLUG>>Second pitch.<</PLUG>>"
    cleaned, span = extract_plug_span(text)
    assert "<<PLUG>>" not in cleaned
    assert "<</PLUG>>" not in cleaned
    start, end = span
    assert cleaned[start:end] == "First pitch."
    captured = capsys.readouterr()
    assert "2 <<PLUG>> marker pairs found" in captured.err
