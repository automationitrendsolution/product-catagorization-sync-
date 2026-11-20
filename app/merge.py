"""Merge rules: four source tables -> the rows the target should hold.

Pure functions over lists of dicts, so they are tested without a database.
Rows are matched on (MarketPlace, Child ASIN), trimmed and upper-cased.

Rules (agreed 2026-11-11):

1. ``product_categorisation``  -> US rows, every target column.
2. The same rows again as CA   -> while ``CA_MIRRORS_US`` is true (how the table
   has always been built).
3. ``product_categorisation_uk`` -> UK rows, every target column.
4. ``product_categorisation_gr`` -> DE rows, the 12 columns it has.
5. ``product_categorisation_non_us`` (only marketplaces in SYNC_MARKETPLACES,
   so UAE is skipped):
     * key already built above -> its Parent ASIN, Product Owner, Product Lead,
       Manager and season override (non-blank values only);
     * new key -> a new row from its 10 columns (child_name / parent_name fill
       both the long and the short name columns).
6. Target rows matching no source are kept and only reported (never deleted).

Only the columns a source manages are compared and written; the others in an
existing target row are left as they are.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

TARGET_COLUMNS: tuple[str, ...] = (
    "Child ASIN", "Parent ASIN", "Child", "Parent", "Product Group - Child",
    "Performance Caegory of Products", "Product Group - Parent", "Product Category",
    "Product Lead", "Product Owner", "Parent Short name", "Child Short Name", "Manager",
    "MarketPlace", "Asst Manager", "PPC Agent", "Performance Category", "Sort",
    "unique_number", "Sub Category", "Image", "Season",
)
KEY_COLUMNS = ("MarketPlace", "Child ASIN")
INT_COLUMNS = frozenset({"Sort"})

#: Columns product_categorisation and product_categorisation_uk carry (all of the target's).
FULL_COLUMNS = TARGET_COLUMNS
GR_COLUMNS: tuple[str, ...] = (
    "Child ASIN", "Parent ASIN", "Child", "Parent", "Product Group - Child",
    "Product Group - Parent", "Parent Short name", "Child Short Name", "MarketPlace",
    "Sub Category", "Image", "Season",
)
#: non_us column -> target column, for a key no other source has.
NON_US_NEW_ROW: dict[str, tuple[str, ...]] = {
    "child_asin": ("Child ASIN",),
    "parent_asin": ("Parent ASIN",),
    "child_name": ("Child", "Child Short Name"),
    "parent_name": ("Parent", "Parent Short name"),
    "product_owner": ("Product Owner",),
    "product_lead": ("Product Lead",),
    "manager": ("Manager",),
    "marketplace": ("MarketPlace",),
    "season": ("Season",),
}
#: non_us column -> target column it overrides on a key another source already built.
NON_US_OVERRIDE: dict[str, str] = {
    "parent_asin": "Parent ASIN",
    "product_owner": "Product Owner",
    "product_lead": "Product Lead",
    "manager": "Manager",
    "season": "Season",
}
MARKETPLACE_ALIASES = {"GB": "UK"}

SRC_US = "product_categorisation"
SRC_GR = "product_categorisation_gr"
SRC_NON_US = "product_categorisation_non_us"
SRC_UK = "product_categorisation_uk"
SOURCES = (SRC_US, SRC_GR, SRC_NON_US, SRC_UK)

Key = tuple[str, str]


def clean(value: Any) -> Any:
    """Trim text; blank text and None are both None."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def clean_int(value: Any) -> int | None:
    """'3', '3.0', 3 -> 3; blank -> None; anything else raises ValueError."""
    value = clean(value)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(value)
    f = float(value)
    if f != int(f):
        raise ValueError(value)
    return int(f)


def normalise(column: str, value: Any) -> Any:
    if column in INT_COLUMNS:
        try:
            return clean_int(value)
        except (TypeError, ValueError):
            return clean(value)  # reported as a difference, never silently dropped
    value = clean(value)
    return str(value) if value is not None and not isinstance(value, str) else value


def marketplace(value: Any) -> str:
    mp = (clean(value) or "").upper()
    return MARKETPLACE_ALIASES.get(mp, mp)


def make_key(mp: Any, asin: Any) -> Key:
    return (marketplace(mp), (clean(asin) or "").upper())


@dataclass
class DesiredRow:
    key: Key
    values: dict[str, Any]          # managed columns only
    sources: list[str] = field(default_factory=list)


@dataclass
class MergeResult:
    rows: dict[Key, DesiredRow]
    warnings: list[str]
    skipped: dict[str, int]         # reason -> count
    source_keys: dict[str, set[Key]]


def _take(row: dict[str, Any], columns: Iterable[str]) -> dict[str, Any]:
    """Managed values of a source row. A column the source table does not have
    (not even as NULL) is not managed, so the target keeps its own value."""
    return {c: normalise(c, row.get(c)) for c in columns if c in row}


def build_desired(sources: dict[str, list[dict[str, Any]]], *, marketplaces: Iterable[str],
                  ca_mirrors_us: bool = True) -> MergeResult:
    """Apply the merge rules to the source rows."""
    allowed = {m.upper() for m in marketplaces}
    rows: dict[Key, DesiredRow] = {}
    warnings: list[str] = []
    skipped: dict[str, int] = {}
    source_keys: dict[str, set[Key]] = {s: set() for s in SOURCES}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    def put(src: str, row: dict[str, Any], columns: tuple[str, ...], *, force_mp: str | None = None) -> None:
        values = _take(row, columns)
        mp = force_mp or marketplace(row.get("MarketPlace"))
        key = make_key(mp, values.get("Child ASIN"))
        if not key[1]:
            skip(f"{src}: blank Child ASIN")
            return
        if not key[0]:
            skip(f"{src}: blank MarketPlace")
            return
        if key[0] not in allowed:
            skip(f"{src}: marketplace {key[0]} not in SYNC_MARKETPLACES")
            return
        values["MarketPlace"] = key[0]
        values["Child ASIN"] = key[1]
        label = src if not force_mp else f"{src} (as {force_mp})"
        if key in rows:
            warnings.append(f"{label}: {key[0]} {key[1]} appears more than once; the later row is used")
            rows[key].values.update(values)
            rows[key].sources.append(label)
        else:
            rows[key] = DesiredRow(key, values, [label])
        source_keys[src].add(key)

    for r in sources.get(SRC_US, []):
        put(SRC_US, r, FULL_COLUMNS)
        if ca_mirrors_us:
            put(SRC_US, r, FULL_COLUMNS, force_mp="CA")
    for r in sources.get(SRC_UK, []):
        put(SRC_UK, r, FULL_COLUMNS)
    for r in sources.get(SRC_GR, []):
        put(SRC_GR, r, GR_COLUMNS)

    # non_us last, highest id last, so for a duplicated key the newest row wins.
    non_us = sorted(sources.get(SRC_NON_US, []), key=lambda r: r.get("id") or 0)
    seen: set[Key] = set()
    for r in non_us:
        key = make_key(r.get("marketplace"), r.get("child_asin"))
        if not key[1]:
            skip(f"{SRC_NON_US}: blank child_asin")
            continue
        if key[0] not in allowed:
            skip(f"{SRC_NON_US}: marketplace {key[0] or '(blank)'} not in SYNC_MARKETPLACES")
            continue
        if key in seen:
            warnings.append(f"{SRC_NON_US}: {key[0]} {key[1]} appears more than once; row id {r.get('id')} (newest) is used")
        seen.add(key)
        source_keys[SRC_NON_US].add(key)
        if key in rows:
            for src_col, tgt_col in NON_US_OVERRIDE.items():
                v = normalise(tgt_col, r.get(src_col))
                if v is not None:
                    rows[key].values[tgt_col] = v
            if SRC_NON_US not in rows[key].sources:
                rows[key].sources.append(SRC_NON_US)
        else:
            values: dict[str, Any] = {}
            for src_col, tgt_cols in NON_US_NEW_ROW.items():
                if src_col not in r:
                    continue  # column missing from the table (e.g. season): left to the target
                for c in tgt_cols:
                    values[c] = normalise(c, r.get(src_col))
            values["MarketPlace"], values["Child ASIN"] = key
            rows[key] = DesiredRow(key, values, [SRC_NON_US])

    return MergeResult(rows, warnings, skipped, source_keys)


# --------------------------------------------------------------------------- diff

@dataclass
class Diff:
    inserts: list[DesiredRow]
    #: (desired row, target row as stored, {column: new value}) for changed columns only
    updates: list[tuple[DesiredRow, dict[str, Any], dict[str, Any]]]
    orphans: list[dict[str, Any]]           # target rows no source has
    duplicate_target_keys: dict[Key, int]
    #: source keys stored more than once in the target: left exactly as they are, only reported
    skipped_duplicates: list[DesiredRow] = field(default_factory=list)

    @property
    def in_sync(self) -> bool:
        """Every key that is unique (or missing) in the target matches the sources."""
        return not self.inserts and not self.updates

    def changed_cells(self) -> int:
        return sum(len(c) for _, _, c in self.updates)


def diff(desired: dict[Key, DesiredRow], target_rows: list[dict[str, Any]]) -> Diff:
    """Compare desired rows with the target.

    Only keys that occur exactly once in the target are updated; a key the
    target holds more than once is left untouched (``skipped_duplicates``),
    because nobody can tell which copy is the right one. A key missing from
    the target is inserted, which keeps it unique.
    """
    by_key: dict[Key, dict[str, Any]] = {}
    dup: dict[Key, int] = {}
    for t in target_rows:
        k = make_key(t.get("MarketPlace"), t.get("Child ASIN"))
        if k in by_key:
            dup[k] = dup.get(k, 1) + 1
        by_key[k] = t

    inserts, updates, skipped = [], [], []
    for key, want in desired.items():
        if key in dup:
            skipped.append(want)
            continue
        have = by_key.get(key)
        if have is None:
            inserts.append(want)
            continue
        changes = {}
        for col, v in want.values.items():
            if col in KEY_COLUMNS:
                continue  # matched case/space-insensitively; never rewritten
            if normalise(col, have.get(col)) != v:
                changes[col] = v
        if changes:
            updates.append((want, have, changes))
    orphans = [t for k, t in by_key.items() if k not in desired]
    return Diff(inserts, updates, orphans, dup, skipped)
