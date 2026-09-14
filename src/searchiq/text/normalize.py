"""Arabic-aware text normalisation.

Arabic queries in the source log vary in ways that carry no meaning: the same
word is written with different alef forms, with or without the final ta marbuta,
with stretched letters, or with Arabic-Indic digits. Comparing raw strings
therefore reports differences that no shopper intended.

Normalisation folds those variants together so that `فراولة` and `فراوله`
("strawberry", written two ways) become one term, while genuine differences —
`حليب` vs `حليبن` — survive and can be measured as the misspellings they are.

The same function normalises product names at ETL time, so a query term and a
catalogue term are always compared in the same form.
"""

from __future__ import annotations

import enum
import re
import unicodedata

# Harakat (short vowels), sukun, shadda, superscript alef. Optional in writing
# and almost never typed into a search box; always stripped.
_ARABIC_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ]")

# Tatweel / kashida: a purely typographic letter-stretcher with no phonetic value.
_TATWEEL = "ـ"

# Letter folding. Each group collapses to the form shoppers type most often.
_LETTER_FOLDING = str.maketrans(
    {
        # Alef with hamza/madda -> bare alef.
        "آ": "ا",  # آ
        "أ": "ا",  # أ
        "إ": "ا",  # إ
        "ٱ": "ا",  # ٱ
        # Alef maqsura -> ya. (`على` vs `علي`)
        "ى": "ي",
        # Ta marbuta -> ha. (`فراولة` vs `فراوله`)
        "ة": "ه",
        # Hamza seats -> their bare carriers.
        "ؤ": "و",  # ؤ
        "ئ": "ي",  # ئ
        # Persian/Urdu look-alikes that reach Arabic keyboards.
        "ک": "ك",  # ک -> ك
        "ی": "ي",  # ی -> ي
    }
)

# Arabic-Indic and Eastern Arabic-Indic digits -> ASCII.
_DIGIT_FOLDING = str.maketrans(
    {chr(0x0660 + i): str(i) for i in range(10)}
    | {chr(0x06F0 + i): str(i) for i in range(10)}
)

# Everything that is not a letter, a digit, or whitespace becomes a space, so
# `جهينه - 1 لتر` and `جهينه 1 لتر` tokenize identically.
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")

_ARABIC_RANGE = re.compile(r"[؀-ۿݐ-ݿ]")
_LATIN_RANGE = re.compile(r"[A-Za-z]")
_DIGIT_RANGE = re.compile(r"[0-9]")


class Script(enum.StrEnum):
    """Which writing system a query is in — drives RTL display and term matching."""

    ARABIC = "arabic"
    LATIN = "latin"
    DIGITS = "digits"
    MIXED = "mixed"
    OTHER = "other"


def normalize(text: str) -> str:
    """Fold `text` to the canonical form used for every comparison in the system.

    Applies, in order: Unicode NFKC, diacritic removal, tatweel removal, Arabic
    letter folding, digit folding, case folding, punctuation-to-space, and
    whitespace collapsing.

    >>> normalize("فَراولة")
    'فراوله'
    >>> normalize("  Juhayna   Milk - 1 L ")
    'juhayna milk 1 l'
    """
    if not text:
        return ""

    text = unicodedata.normalize("NFKC", text)
    text = _ARABIC_DIACRITICS.sub("", text)
    text = text.replace(_TATWEEL, "")
    text = text.translate(_LETTER_FOLDING)
    text = text.translate(_DIGIT_FOLDING)
    text = text.casefold()
    text = _NON_WORD.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def tokenize(text: str, *, min_length: int = 2) -> list[str]:
    """Normalise `text` and split it into terms.

    Tokens shorter than `min_length` are dropped: single characters carry no
    retrieval signal and would dominate every frequency count. Digits are kept
    because grocery names are full of meaningful sizes (`1 لتر`, `400 gr`).
    """
    return [tok for tok in normalize(text).split() if len(tok) >= min_length]


# Arabic clitics that attach to the front of a noun. `الفاكهة` ("the fruit")
# and `فاكهة` ("fruit") are the same product term, but they tokenize
# differently, so the catalogue would under-report coverage for every shopper
# who omits the article. Ordered longest-first so `وال` is tried before `ال`.
_ARABIC_PREFIXES = ("وبال", "فبال", "وال", "بال", "كال", "فال", "لل", "ال")

# Below this length the remainder is more likely to be a coincidence than a
# stem, so the prefix is left alone.
_MIN_STEM_LENGTH = 3


def light_stem(token: str) -> str:
    """Strip a leading Arabic article or preposition clitic from `token`.

    Deliberately *not* part of `normalize`: normalisation must stay predictable
    enough to show a shopper their own query back. Stemming is lossy, so it is
    applied only where the goal is matching a query term against catalogue
    vocabulary.

    Without a lexicon this cannot be exact. `الفاكهه` correctly yields `فاكهه`,
    but `الوان` ("colours", where the alef-lam is part of the word) yields the
    non-word `وان`. That asymmetry is why callers should use `term_variants`,
    which keeps both forms: an unused extra index entry costs nothing, whereas a
    missed stem silently under-reports catalogue coverage.

    >>> light_stem("الفاكهه")
    'فاكهه'
    >>> light_stem("milk")
    'milk'
    """
    for prefix in _ARABIC_PREFIXES:
        if token.startswith(prefix) and len(token) - len(prefix) >= _MIN_STEM_LENGTH:
            return token[len(prefix):]
    return token


def term_variants(token: str) -> set[str]:
    """Every form `token` should be indexed or looked up under.

    Returns the token itself plus its light-stemmed form, so a query for
    `فاكهه` matches a product named `كوكتيل الفاكهة` and vice versa.
    """
    return {token, light_stem(token)}


# Unicode bidirectional isolates. `FSI` opens a run whose direction is decided
# by its own first strong character; `PDI` closes it.
_FSI, _PDI = "⁨", "⁩"


def isolate(text: str) -> str:
    """Wrap `text` so surrounding characters cannot be dragged into its direction.

    Without this, an Arabic term inside an English sentence pulls the neutral
    characters beside it — digits, quotes, dashes — into the right-to-left run.
    "a misspelling of “حليب”, which 322 products use" renders as
    "a misspelling of 322 ,”حليب“ products use": the sentence is intact in
    memory and wrong on screen.

    Applied at the point a term is embedded in prose, so every consumer (the
    dashboard, the terminal, the Markdown digest, the API) gets correct text
    without each having to know about bidirectional layout.
    """
    return f"{_FSI}{text}{_PDI}"


def strip_isolates(text: str) -> str:
    """Remove bidirectional isolate marks from `text`.

    Browsers and bidi-aware renderers honour the marks and show nothing; a plain
    terminal has no such layout engine and prints them as stray glyphs. Output
    bound for a console is therefore stripped, while the same string keeps its
    isolates everywhere it will actually be laid out.
    """
    return text.replace(_FSI, "").replace(_PDI, "")


def script_of(text: str) -> Script:
    """Classify the writing system of `text`.

    Used to keep Arabic and Latin vocabularies apart (an Arabic query is never a
    misspelling of an English product name) and to set `dir="rtl"` in the UI.
    """
    if not text.strip():
        return Script.OTHER

    has_arabic = bool(_ARABIC_RANGE.search(text))
    has_latin = bool(_LATIN_RANGE.search(text))
    has_digits = bool(_DIGIT_RANGE.search(text))

    if has_arabic and has_latin:
        return Script.MIXED
    if has_arabic:
        return Script.ARABIC
    if has_latin:
        return Script.LATIN
    if has_digits:
        return Script.DIGITS
    return Script.OTHER
