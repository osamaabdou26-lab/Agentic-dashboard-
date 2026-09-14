"""Text normalisation and string-similarity primitives (Arabic-aware)."""

from searchiq.text.normalize import (
    Script,
    isolate,
    light_stem,
    normalize,
    script_of,
    strip_isolates,
    term_variants,
    tokenize,
)
from searchiq.text.similarity import (
    damerau_levenshtein,
    is_prefix_of,
    similarity,
)

__all__ = [
    "Script",
    "damerau_levenshtein",
    "is_prefix_of",
    "isolate",
    "light_stem",
    "normalize",
    "script_of",
    "similarity",
    "strip_isolates",
    "term_variants",
    "tokenize",
]
