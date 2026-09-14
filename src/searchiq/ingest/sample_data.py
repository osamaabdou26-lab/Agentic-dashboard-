"""Generate a realistic mock dataset, so the system works with no source dump.

The shipped project is built against an 828 MB grocery dump that cannot travel
with the code. Without it `searchiq etl` has nothing to read, and a reviewer
cloning the repository sees an empty dashboard — which says nothing about
whether any of this works.

This module writes a *mysqldump-format SQL file*, not rows straight into the
store. That is the point: the generated file is consumed by the same
`ingest.dump_reader` -> `ingest.loader` path production uses, so what a reviewer
exercises out of the box is the real ETL, not a fixture-shaped shortcut around
it. The only thing mocked is the source data.

The log is written to contain, deliberately and in known proportions, every
pattern the analytics layer claims to detect:

* **zero-result queries** — terms the catalogue does not stock at all
* **misspellings** — a typo that returns the wrong aisle, then the shopper
  retyping it correctly in the same session
* **partial queries** — `pas` before `pasta`, which must *not* be reported as a
  spelling mistake
* **low-engagement terms** — results returned, then repeated and reworded,
  which is the only dissatisfaction signal this data model has
* **cross-language synonyms** — every product carries an Arabic and an English
  name, so `لبن ≡ حليب` is induced from the catalogue rather than assumed
* **machine-like traffic** — an identical search re-fired inside two seconds,
  which must be excluded from engagement rather than counted as frustration

Generation is seeded, so the same seed produces identical output and two people
comparing dashboards are looking at the same numbers.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_SEED = 20250109
DEFAULT_DAYS = 28
DEFAULT_SESSIONS_PER_DAY = 6

# The search engine being modelled is embedding-based and returns its nearest
# neighbours, so a populated result set is normally full. Under-filled sets are
# generated deliberately, not as noise.
_RESULT_CAP = 5


@dataclass(frozen=True)
class Family:
    """One product concept, in both languages, with the brands that carry it.

    `ar` and `en` are the terms a shopper actually types. They appear in every
    generated product name for the family, which is what gives the catalogue
    enough support for a correction to be proposed towards them, and what makes
    the two terms co-occur often enough to be aligned across languages.
    """

    key: str
    ar: str
    en: str
    category_ar: str
    category_en: str
    brands: tuple[tuple[str, str], ...]
    variants: tuple[tuple[str, str], ...]
    # A second Arabic word for the same concept, used on a slice of the
    # products. This is how `لبن` and `حليب` both align to `milk`, and so to
    # each other, without a dictionary.
    ar_alias: str | None = None


_FAMILIES: tuple[Family, ...] = (
    Family(
        key="milk",
        ar="حليب",
        en="Milk",
        category_ar="منتجات الألبان",
        category_en="Dairy",
        brands=(("جهينه", "Juhayna"), ("المراعي", "Almarai"), ("بخيره", "Bekhero"),
                ("دينا", "Dina Farms"), ("لاكتيل", "Lactel")),
        variants=(("كامل الدسم", "Full Cream"),
                  ("خالي الدسم", "Skimmed"),
                  ("قليل الدسم", "Low Fat"),
                  ("مكثف محلى", "Sweetened Condensed")),
        ar_alias="لبن",
    ),
    Family(
        key="strawberry",
        ar="فراولة",
        en="Strawberry",
        category_ar="فواكه",
        category_en="Fruit",
        brands=(("ميرو", "Mero"), ("جيفريكس", "Givrex"), ("دلتا", "Delta"),
                ("الوادي", "El Wadi"), ("باسم", "Basem")),
        variants=(("طازجة", "Punnet"),
                  ("مجمدة", "Frozen"),
                  ("مربى", "Jam"),
                  ("عصير", "Juice")),
    ),
    Family(
        key="pasta",
        ar="مكرونة",
        en="Pasta",
        category_ar="بقالة جافة",
        category_en="Dry Grocery",
        brands=(("الملكة", "El Malika"), ("اجنيزي", "Agnesi"), ("ريجينا", "Regina"),
                ("الاسرة", "El Osra"), ("باريلا", "Barilla")),
        variants=(("اسباجيتي", "Spaghetti"),
                  ("بيني", "Penne"),
                  ("فارفالي", "Farfalle"),
                  ("شعرية", "Vermicelli")),
    ),
    Family(
        key="cheese",
        ar="جبنة",
        en="Cheese",
        category_ar="منتجات الألبان",
        category_en="Dairy",
        brands=(("دومتي", "Domty"), ("بانوفا", "Panova"), ("الريف", "El Rif"),
                ("بريزيدون", "President"), ("لافاش", "La Vache")),
        variants=(("بيضاء", "White"),
                  ("رومي", "Roumy"),
                  ("شيدر مبشورة", "Shredded Cheddar"),
                  ("مثلثات", "Triangles")),
    ),
    Family(
        key="chicken",
        ar="دجاج",
        en="Chicken",
        category_ar="لحوم ودواجن",
        category_en="Meat & Poultry",
        brands=(("كوكي", "Koki"), ("الوطنية", "El Watania"), ("حلواني", "Halwani"),
                ("سيرا", "Sera"), ("كوكب", "Kawkab")),
        variants=(("صدور مجمدة", "Frozen Breast"),
                  ("بانيه", "Pane"),
                  ("كامل مبرد", "Whole Chilled"),
                  ("ناجتس", "Nuggets")),
        ar_alias="فراخ",
    ),
    Family(
        key="rice",
        ar="أرز",
        en="Rice",
        category_ar="بقالة جافة",
        category_en="Dry Grocery",
        brands=(("الضحى", "El Doha"), ("ابو كاس", "Abu Kass"), ("الملكة", "El Malika"),
                ("سنابل", "Sanabel"), ("انكل بنز", "Uncle Bens")),
        variants=(("مصري", "Egyptian"),
                  ("بسمتي", "Basmati"),
                  ("مطحون", "Ground"),
                  ("بني", "Brown")),
    ),
    Family(
        key="oil",
        ar="زيت",
        en="Oil",
        category_ar="بقالة جافة",
        category_en="Dry Grocery",
        brands=(("كريستال", "Crystal"), ("عافية", "Afia"), ("سلطان", "Sultan"),
                ("الضحى", "El Doha"), ("بورجيس", "Borges")),
        variants=(("عباد الشمس", "Sunflower"),
                  ("ذرة", "Corn"),
                  ("زيتون بكر", "Extra Virgin Olive"),
                  ("نخيل", "Palm")),
    ),
    Family(
        key="tea",
        ar="شاي",
        en="Tea",
        category_ar="مشروبات",
        category_en="Beverages",
        brands=(("العروسة", "El Arosa"), ("ليبتون", "Lipton"), ("تويننجز", "Twinings"),
                ("مذاق", "Mazaq"), ("الزهور", "El Zohour")),
        variants=(("ناعم", "Fine"),
                  ("فتلة", "Bags"),
                  ("أخضر", "Green"),
                  ("بالنعناع", "With Mint")),
    ),
    Family(
        key="yoghurt",
        ar="زبادي",
        en="Yoghurt",
        category_ar="منتجات الألبان",
        category_en="Dairy",
        brands=(("جهينه", "Juhayna"), ("المراعي", "Almarai"), ("دانون", "Danone"),
                ("بيتي", "Beyti"), ("دينا", "Dina Farms")),
        variants=(("طبيعي", "Plain"),
                  ("بالفواكه", "With Fruit"),
                  ("يوناني", "Greek"),
                  ("لايت", "Light")),
    ),
    Family(
        key="bread",
        ar="خبز",
        en="Bread",
        category_ar="مخبوزات",
        category_en="Bakery",
        brands=(("الرشيدي", "El Rashidi"), ("ريتش بيك", "Rich Bake"), ("فينو", "Fino"),
                ("التوحيد", "El Tawheed"), ("بيك رولز", "Bake Rolls")),
        variants=(("توست أبيض", "White Toast"),
                  ("بلدي", "Baladi"),
                  ("سن", "Brown"),
                  ("برجر", "Burger Buns")),
    ),
    Family(
        key="coffee",
        ar="قهوة",
        en="Coffee",
        category_ar="مشروبات",
        category_en="Beverages",
        brands=(("العبد", "El Abd"), ("نسكافيه", "Nescafe"), ("شاهين", "Shaheen"),
                ("الصفا", "El Safa"), ("جولد", "Gold")),
        variants=(("تركي محوج", "Turkish Spiced"),
                  ("سريع الذوبان", "Instant"),
                  ("حبوب محمصة", "Roasted Beans"),
                  ("بالحليب 3 في 1", "3In1 With Milk")),
    ),
    Family(
        key="juice",
        ar="عصير",
        en="Juice",
        category_ar="مشروبات",
        category_en="Beverages",
        brands=(("جهينه", "Juhayna"), ("بيتي", "Beyti"), ("فاميلي", "Family"),
                ("الوادي", "El Wadi"), ("كاريبو", "Caribou")),
        variants=(("مانجو", "Mango"),
                  ("برتقال", "Orange"),
                  ("جوافة", "Guava"),
                  ("فراولة", "Strawberry")),
    ),
)

# Pack sizes, drawn from one shared pool rather than fixed per family.
# Spreading every unit across every aisle is what stops a measurement word
# becoming a family's strongest cross-language signal: `kg` on a handful of
# chicken products would otherwise "translate" to `كجم` more confidently than
# `chicken` translates to `دجاج`, and two families sharing a unit would be
# proposed as synonyms of each other.
_SIZES: tuple[tuple[str, str], ...] = (
    ("1 لتر", "1 L"),
    ("500 مل", "500 Ml"),
    ("250 جم", "250 Gr"),
    ("1 كجم", "1 Kg"),
    ("6 قطع", "6 Pcs"),
    ("400 جم", "400 Gr"),
    ("2 لتر", "2 L"),
    ("100 مل", "100 Ml"),
    ("750 جم", "750 Gr"),
    ("12 قطعة", "12 Pcs"),
    ("200 مل", "200 Ml"),
    ("1.5 كجم", "1.5 Kg"),
)


# Terms the catalogue does not stock, in either language. These produce genuine
# zero-result searches, and the term rollup should call them assortment gaps
# rather than blaming retrieval. Kept lexically far from every catalogue term so
# the misspelling layer cannot mistake one for a typo.
_UNSTOCKED: tuple[str, ...] = (
    "kombucha",
    "quinoa",
    "wasabi",
    "truffle",
    "كافيار",
    "زعفران",
    "كينوا",
)

# (typed, intended family key). The typed form is absent from the catalogue and
# within a short edit distance of a term the catalogue uses heavily.
_TYPOS: tuple[tuple[str, str], ...] = (
    ("حليبن", "milk"),
    ("فاراوله", "strawberry"),
    ("فراولت", "strawberry"),
    ("مكرونا", "pasta"),
    ("جبنا", "cheese"),
    ("chiken", "chicken"),
    ("stawberry", "strawberry"),
    ("yoghrt", "yoghurt"),
    ("coffe", "coffee"),
)

# (typed prefix, intended family key). A shopper who stopped typing. Rewriting
# their query would be wrong, so these must be classified apart from typos.
_PARTIALS: tuple[tuple[str, str], ...] = (
    ("pas", "pasta"),
    ("straw", "strawberry"),
    ("chick", "chicken"),
    ("زبا", "yoghurt"),
)

# (typed, family key) for terms that return *relevant* products and are still
# re-searched. Keeping them relevant is what makes them a distinct signal: the
# results mention the query, so nothing lexical is wrong, and only the
# behavioural proxy and the short result set say the shopper was not served.
_LOW_ENGAGEMENT: tuple[tuple[str, str], ...] = (
    ("عصير", "juice"),
    ("juice", "juice"),
    ("خبز", "bread"),
    ("bread", "bread"),
    ("شاي", "tea"),
)

# Qualifiers a dissatisfied shopper appends when the first attempt disappoints.
_REFINEMENTS: tuple[str, ...] = ("طازج", "fresh", "offer")


@dataclass
class SampleReport:
    """What a generation run produced, for the CLI to echo."""

    path: Path
    products: int = 0
    categories: int = 0
    searches: int = 0
    sessions: int = 0
    zero_result_searches: int = 0
    typo_searches: int = 0
    partial_searches: int = 0
    automated_searches: int = 0
    period_start: str = ""
    period_end: str = ""

    def as_lines(self) -> list[str]:
        return [
            f"file:   {self.path}",
            f"period: {self.period_start} to {self.period_end}",
            f"  {'products':<22} {self.products:>7,}",
            f"  {'categories':<22} {self.categories:>7,}",
            f"  {'searches':<22} {self.searches:>7,}",
            f"  {'sessions':<22} {self.sessions:>7,}",
            "planted patterns:",
            f"  {'zero-result searches':<22} {self.zero_result_searches:>7,}",
            f"  {'misspelled searches':<22} {self.typo_searches:>7,}",
            f"  {'partial queries':<22} {self.partial_searches:>7,}",
            f"  {'machine-like repeats':<22} {self.automated_searches:>7,}",
        ]


@dataclass
class _Product:
    id: int
    sku: str
    url_key: str
    price: float
    name_ar: str
    name_en: str
    category_id: int
    family: str


def generate_sample_dump(
    output_path: Path | str,
    *,
    days: int = DEFAULT_DAYS,
    sessions_per_day: int = DEFAULT_SESSIONS_PER_DAY,
    seed: int = DEFAULT_SEED,
    end: datetime | None = None,
) -> SampleReport:
    """Write a mock `mysqldump` file to `output_path` and describe what is in it.

    The file is complete input for `searchiq etl --dump <output_path>`: the same
    statement shapes, column orders and escaping rules as the real dump.
    """
    if days < 2:
        raise ValueError("days must be at least 2, so the digest has a baseline period")
    if sessions_per_day < 1:
        raise ValueError("sessions_per_day must be at least 1")

    output_path = Path(output_path)
    rng = random.Random(seed)

    # Anchored to a fixed hour so a regenerated file differs only where the seed
    # says it should, never because of the time of day it was run.
    end = (end or datetime.now()).replace(hour=20, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)

    categories, products = _build_catalogue()
    searches, counts, sessions = _build_search_log(
        products, rng=rng, start=start, days=days, sessions_per_day=sessions_per_day
    )
    query_counts = _build_query_counts(searches)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        _render_dump(categories, products, searches, query_counts), encoding="utf-8"
    )

    return SampleReport(
        path=output_path,
        products=len(products),
        categories=len(categories),
        searches=len(searches),
        sessions=sessions,
        period_start=searches[0][4][:19] if searches else "",
        period_end=searches[-1][4][:19] if searches else "",
        **counts,
    )


# catalogue
def _build_catalogue() -> tuple[list[tuple[int, str, str]], list[_Product]]:
    """Cross every family with its brands and variants.

    Brands and variants are crossed rather than sampled so each family lands on
    a predictable product count — high enough for a correction towards its term
    to clear the catalogue-support bar, and even enough across families that no
    term wins on volume alone.
    """
    categories: list[tuple[int, str, str]] = []
    category_ids: dict[str, int] = {}
    products: list[_Product] = []

    for family in _FAMILIES:
        if family.category_en not in category_ids:
            category_ids[family.category_en] = len(category_ids) + 1
            categories.append(
                (category_ids[family.category_en], family.category_ar, family.category_en)
            )

    product_id = 400_000
    for family in _FAMILIES:
        category_id = category_ids[family.category_en]
        for brand_index, (brand_ar, brand_en) in enumerate(family.brands):
            for variant_ar, variant_en in family.variants:
                product_id += 1
                # A slice of the range carries the alias spelling instead of the
                # primary one. Both then co-occur with the same English term,
                # which is what lets the aligner infer they mean the same thing.
                use_alias = family.ar_alias is not None and brand_index % 3 == 2
                term_ar = family.ar_alias if use_alias else family.ar
                size_ar, size_en = _SIZES[len(products) % len(_SIZES)]
                slug = f"{family.key}-{brand_en}-{variant_en}".casefold()
                products.append(
                    _Product(
                        id=product_id,
                        sku=f"1{product_id}",
                        url_key="".join(
                            character if character.isalnum() else "-"
                            for character in slug
                        ),
                        price=round(18.0 + (product_id % 47) * 3.5, 2),
                        name_ar=f"{term_ar} {brand_ar} {variant_ar} - {size_ar}",
                        name_en=f"{brand_en} {family.en} {variant_en} - {size_en}",
                        category_id=category_id,
                        family=family.key,
                    )
                )

    return categories, products


# search log
def _build_search_log(
    products: list[_Product],
    *,
    rng: random.Random,
    start: datetime,
    days: int,
    sessions_per_day: int,
) -> tuple[list[tuple], dict[str, int], int]:
    """Lay scripted shopper journeys down across the period.

    Each session is one journey built from a scenario, so the sequence of
    searches inside it carries the behavioural signal — the retype after a typo,
    the reword after an unsatisfying result set — that a shuffled bag of
    independent queries would destroy.
    """
    by_family: dict[str, list[_Product]] = {}
    for product in products:
        by_family.setdefault(product.family, []).append(product)
    family_keys = sorted(by_family)

    events: list[tuple[datetime, str, list[_Product], int | None]] = []
    counts = {
        "zero_result_searches": 0,
        "typo_searches": 0,
        "partial_searches": 0,
        "automated_searches": 0,
    }

    # Scenario mix, weighted so healthy traffic dominates: a log that is mostly
    # broken would make every rate meaningless.
    scenarios = (
        ("healthy", 34),
        ("typo", 16),
        ("low_engagement", 14),
        ("zero_result", 12),
        ("synonym_reword", 10),
        ("partial", 8),
        ("automated", 6),
    )
    population = [name for name, weight in scenarios for _ in range(weight)]

    # Planted patterns are dealt round-robin rather than sampled. Sampling would
    # leave whichever typo the die never picked absent from the log, and a
    # dataset that only *usually* contains what it advertises is not a fixture
    # anyone can test or demonstrate against.
    cursors: dict[str, int] = {"typo": 0, "partial": 0, "unstocked": 0, "low": 0}

    sessions = 0
    for day in range(days):
        day_start = start + timedelta(days=day)
        for session_index in range(sessions_per_day):
            sessions += 1
            # Sessions are spaced well beyond the 30-minute gap that defines a
            # session boundary, so the loader derives the sessions intended here.
            clock = day_start + timedelta(
                hours=session_index * (11 / sessions_per_day) + rng.uniform(0, 0.4)
            )
            events.extend(
                _run_scenario(
                    rng.choice(population),
                    clock=clock,
                    rng=rng,
                    by_family=by_family,
                    family_keys=family_keys,
                    counts=counts,
                    cursors=cursors,
                )
            )

    events.sort(key=lambda item: item[0])
    rows = [
        (
            index,
            query,
            "\n".join(product.name_ar for product in results),
            "\n".join(product.name_en for product in results),
            occurred.strftime("%Y-%m-%d %H:%M:%S.%f"),
            len(results) if forced_count is None else forced_count,
        )
        for index, (occurred, query, results, forced_count) in enumerate(events, start=1)
    ]
    return rows, counts, sessions


def _run_scenario(
    scenario: str,
    *,
    clock: datetime,
    rng: random.Random,
    by_family: dict[str, list[_Product]],
    family_keys: list[str],
    counts: dict[str, int],
    cursors: dict[str, int],
) -> Iterator[tuple[datetime, str, list[_Product], int | None]]:
    """Yield the searches of one shopper journey, in order."""
    family_by_key = {family.key: family for family in _FAMILIES}

    def next_of(name: str, items: tuple) -> Any:
        """Take the next planted pattern of its kind, wrapping around."""
        item = items[cursors[name] % len(items)]
        cursors[name] += 1
        return item

    def hits(key: str, count: int = _RESULT_CAP) -> list[_Product]:
        pool = by_family[key]
        return rng.sample(pool, min(count, len(pool)))

    def misses(exclude: str, count: int = _RESULT_CAP) -> list[_Product]:
        """The wrong aisle: what an embedding engine returns for a term it
        cannot match. Never empty — this engine always answers with something."""
        others = [key for key in family_keys if key != exclude]
        picked: list[_Product] = []
        while len(picked) < count and others:
            picked.extend(hits(rng.choice(others), 1))
        return picked[:count]

    def term(key: str) -> str:
        family = family_by_key[key]
        return rng.choice([family.ar, family.en.casefold()])

    if scenario == "healthy":
        key = rng.choice(family_keys)
        yield (clock, term(key), hits(key), None)
        if rng.random() < 0.6:
            other = rng.choice(family_keys)
            # Well past the five-minute reaction window: the next item on the
            # shopping list, not a reaction to the previous results.
            yield (
                clock + timedelta(seconds=rng.randint(420, 900)),
                term(other),
                hits(other),
                None,
            )

    elif scenario == "typo":
        typed, key = next_of("typo", _TYPOS)
        counts["typo_searches"] += 1
        yield (clock, typed, misses(key), None)
        # The retype. A shopper fixing it themselves in the same breath is the
        # strongest evidence discovery can have for the direction of a
        # correction, so most typo journeys carry it and some do not.
        if rng.random() < 0.75:
            yield (
                clock + timedelta(seconds=rng.randint(8, 70)),
                term(key),
                hits(key),
                None,
            )

    elif scenario == "zero_result":
        counts["zero_result_searches"] += 1
        yield (clock, next_of("unstocked", _UNSTOCKED), [], 0)
        if rng.random() < 0.5:
            counts["zero_result_searches"] += 1
            yield (
                clock + timedelta(seconds=rng.randint(15, 120)),
                next_of("unstocked", _UNSTOCKED),
                [],
                0,
            )

    elif scenario == "low_engagement":
        typed, key = next_of("low", _LOW_ENGAGEMENT)
        # Under-filled and unchanging: the engine could not fill five slots, and
        # returns the same short set however often it is asked.
        stuck = hits(key, rng.randint(2, 3))
        yield (clock, typed, stuck, None)
        yield (clock + timedelta(seconds=rng.randint(6, 40)), typed, stuck, None)
        if rng.random() < 0.6:
            yield (
                clock + timedelta(seconds=rng.randint(50, 200)),
                f"{typed} {rng.choice(_REFINEMENTS)}",
                hits(key, rng.randint(1, 3)),
                None,
            )

    elif scenario == "partial":
        typed, key = next_of("partial", _PARTIALS)
        counts["partial_searches"] += 1
        yield (clock, typed, misses(key, rng.randint(1, 3)), None)
        yield (clock + timedelta(seconds=rng.randint(3, 25)), term(key), hits(key), None)

    elif scenario == "synonym_reword":
        family = rng.choice([family for family in _FAMILIES if family.ar_alias])
        first, second = rng.choice(
            [(family.ar_alias, family.ar), (family.ar, family.en.casefold())]
        )
        yield (clock, first, hits(family.key, rng.randint(3, 5)), None)
        yield (
            clock + timedelta(seconds=rng.randint(10, 90)),
            second,
            hits(family.key),
            None,
        )

    elif scenario == "automated":
        key = rng.choice(family_keys)
        typed = term(key)
        identical = hits(key)
        counts["automated_searches"] += 2
        yield (clock, typed, identical, None)
        # Under two seconds with an identical result set: a retry loop or a test
        # harness, and excluded from engagement rather than read as frustration.
        yield (
            clock + timedelta(milliseconds=rng.randint(200, 1400)),
            typed,
            identical,
            None,
        )


def _build_query_counts(searches: list[tuple]) -> list[tuple[str, int]]:
    """The source keeps its own per-query tally; mirror it from the log."""
    tally: dict[str, int] = {}
    for _, query, _, _, _, _ in searches:
        tally[query] = tally.get(query, 0) + 1
    return sorted(tally.items(), key=lambda item: (-item[1], item[0]))


# rendering
def _escape(value: str) -> str:
    """Escape a Python string the way `mysqldump` would."""
    return (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def _render_dump(
    categories: list[tuple[int, str, str]],
    products: list[_Product],
    searches: list[tuple],
    query_counts: list[tuple[str, int]],
) -> str:
    """Render everything as extended `INSERT` statements.

    Column orders match the production dump exactly, including the columns the
    loader skips: reading position 5 for a URL key is only correct if the
    padding columns are there too.
    """
    stamp = "2025-01-01 00:00:00.000000"

    category_rows = ",".join(
        f"({cid},NULL,'{stamp}','cat-{cid}','{stamp}')" for cid, _, _ in categories
    )
    category_name_rows = ",".join(
        f"({i},'{_escape(ar)}','Arabic',{cid}),"
        f"({i + 5000},'{_escape(en)}','English',{cid})"
        for i, (cid, ar, en) in enumerate(categories, start=1)
    )
    product_rows = ",".join(
        f"({p.id},'{stamp}','{p.sku}',NULL,'{stamp}','{p.url_key}',{p.price},NULL)"
        for p in products
    )
    product_name_rows = ",".join(
        f"({i},'{_escape(p.name_ar)}','Arabic','{stamp}',{p.id}),"
        f"({i + 100000},'{_escape(p.name_en)}','English','{stamp}',{p.id})"
        for i, p in enumerate(products, start=1)
    )
    product_category_rows = ",".join(
        f"({i},{p.id},{p.category_id})" for i, p in enumerate(products, start=1)
    )
    search_rows = ",".join(
        f"({sid},'{_escape(query)}','{_escape(ar)}','{_escape(en)}',{count},'{ts}')"
        for sid, query, ar, en, ts, count in searches
    )
    query_count_rows = ",".join(
        f"({i},'{_escape(query)}',{count})"
        for i, (query, count) in enumerate(query_counts, start=1)
    )

    return "\n".join(
        [
            "-- searchiq generated sample data. Mock source, real ETL path.",
            "-- Regenerate with: searchiq sample-data",
            "/*!40101 SET NAMES utf8mb4 */;",
            f"INSERT INTO `catalog_category` VALUES {category_rows};",
            f"INSERT INTO `catalog_categoryname` VALUES {category_name_rows};",
            f"INSERT INTO `catalog_product` VALUES {product_rows};",
            f"INSERT INTO `catalog_productname` VALUES {product_name_rows};",
            f"INSERT INTO `catalog_product_categories` VALUES {product_category_rows};",
            # Present in the real dump and deliberately outside the loader's
            # whitelist: the reader must skip it without parsing it.
            "INSERT INTO `recommendations_productembedding` VALUES "
            "(1,400001,'sample','0.01,0.02,0.03','ar');",
            f"INSERT INTO `recommendations_querycount` VALUES {query_count_rows};",
            f"INSERT INTO `recommendations_searches` VALUES {search_rows};",
            "-- Dump completed",
            "",
        ]
    )
