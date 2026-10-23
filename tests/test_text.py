from bidpilot.text import clean_text, count_emoji, persian_ratio, to_ascii_digits, to_persian_digits, truncate


def test_digits_roundtrip():
    assert to_ascii_digits("۱۲۳۴۵۶۷۸۹۰") == "1234567890"
    assert to_ascii_digits("١٢٣") == "123"
    assert to_persian_digits("2026") == "۲۰۲۶"


def test_clean_text_normalizes_arabic_characters_and_entities():
    raw = "علي &amp; كامپيوتر\u00a0  با   فاصله\u200c"
    cleaned = clean_text(raw)
    assert "ي" not in cleaned and "ك" not in cleaned
    assert "&amp;" not in cleaned
    assert "  " not in cleaned


def test_persian_ratio():
    assert persian_ratio("سلام دنیا") == 1.0
    assert persian_ratio("hello world") == 0.0
    assert 0 < persian_ratio("سلام hello") < 1


def test_truncate_keeps_words():
    assert truncate("یک دو سه چهار", 9).endswith("…")
    assert len(truncate("a" * 50, 10)) == 10


def test_count_emoji():
    assert count_emoji("سلام 🚀") == 1
    assert count_emoji("بدون ایموجی") == 0
