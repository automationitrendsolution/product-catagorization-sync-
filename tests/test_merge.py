"""Merge-rule tests. No database."""

from __future__ import annotations

from app.merge import (SRC_GR, SRC_NON_US, SRC_UK, SRC_US, TARGET_COLUMNS, build_desired, diff,
                       normalise)

MPS = ("US", "CA", "UK", "IT", "DE")


def full(asin, mp, **kw):
    row = {c: None for c in TARGET_COLUMNS}
    row.update({"Child ASIN": asin, "MarketPlace": mp, "Child": f"name {asin}", "Product Owner": "A", "Sort": 3})
    row.update(kw)
    return row


def non_us(id_, asin, mp, **kw):
    row = {"id": id_, "child_asin": asin, "parent_asin": "P" + asin, "child_name": "short", "parent_name": "par",
           "product_owner": "Ashwin", "product_lead": "L", "manager": "M", "marketplace": mp}
    row.update(kw)
    return row


def merged(**sources):
    return build_desired({SRC_US: [], SRC_GR: [], SRC_NON_US: [], SRC_UK: [], **sources}, marketplaces=MPS)


def test_us_rows_are_mirrored_as_ca():
    m = merged(**{SRC_US: [full("B1", "US")]})
    assert set(m.rows) == {("US", "B1"), ("CA", "B1")}
    assert m.rows[("CA", "B1")].values["MarketPlace"] == "CA"
    assert m.rows[("CA", "B1")].values["Child"] == "name B1"


def test_ca_mirror_can_be_switched_off():
    m = build_desired({SRC_US: [full("B1", "US")]}, marketplaces=MPS, ca_mirrors_us=False)
    assert set(m.rows) == {("US", "B1")}


def test_non_us_overrides_people_and_parent_but_not_names():
    m = merged(**{SRC_UK: [full("B2", "UK", **{"Product Owner": "Ajai"})],
                  SRC_NON_US: [non_us(1, "B2", "UK")]})
    v = m.rows[("UK", "B2")].values
    assert v["Product Owner"] == "Ashwin" and v["Parent ASIN"] == "PB2" and v["Manager"] == "M"
    assert v["Child"] == "name B2"  # long name from _uk kept


def test_blank_non_us_value_does_not_override():
    m = merged(**{SRC_UK: [full("B2", "UK", **{"Product Owner": "Ajai"})],
                  SRC_NON_US: [non_us(1, "B2", "UK", product_owner="  ")]})
    assert m.rows[("UK", "B2")].values["Product Owner"] == "Ajai"


def test_non_us_only_key_becomes_new_row_and_uae_is_skipped():
    m = merged(**{SRC_NON_US: [non_us(1, "B3", "IT"), non_us(2, "B4", "UAE")]})
    assert set(m.rows) == {("IT", "B3")}
    v = m.rows[("IT", "B3")].values
    assert v["Child"] == v["Child Short Name"] == "short"
    assert v["Parent"] == v["Parent Short name"] == "par"
    assert m.skipped == {f"{SRC_NON_US}: marketplace UAE not in SYNC_MARKETPLACES": 1}


def test_non_us_duplicate_key_newest_id_wins():
    m = merged(**{SRC_NON_US: [non_us(660, "B5", "UK", parent_asin="NEW"), non_us(512, "B5", "UK", parent_asin="OLD")]})
    assert m.rows[("UK", "B5")].values["Parent ASIN"] == "NEW"
    assert any("more than once" in w for w in m.warnings)


def test_gr_rows_only_manage_their_columns():
    m = merged(**{SRC_GR: [{"Child ASIN": "B6", "MarketPlace": "DE", "Child": "x"}]})
    assert "Product Owner" not in m.rows[("DE", "B6")].values


def test_keys_are_trimmed_and_case_insensitive():
    m = merged(**{SRC_UK: [full(" b7 ", "uk")]})
    d = diff(m.rows, [full("B7", "UK", Child="name  b7 ".strip())])
    assert not d.inserts


def test_diff_insert_update_orphan_and_blank_equals_null():
    m = merged(**{SRC_UK: [full("B8", "UK", Image=""), full("B9", "UK")]})
    target = [full("B8", "UK", Image=None, **{"Product Owner": "old", "Sort": "3"}),
              full("ZZ", "UK")]
    d = diff(m.rows, target)
    assert [r.key for r in d.inserts] == [("UK", "B9")]
    assert len(d.updates) == 1 and d.updates[0][2] == {"Product Owner": "A"}  # '' == NULL, '3' == 3
    assert [o["Child ASIN"] for o in d.orphans] == ["ZZ"]
    assert not d.in_sync


def test_duplicate_target_keys_are_detected():
    m = merged(**{SRC_UK: [full("B8", "UK")]})
    d = diff(m.rows, [full("B8", "UK"), full("b8 ", "UK")])
    assert d.duplicate_target_keys == {("UK", "B8"): 2}


def test_duplicated_target_key_is_skipped_not_updated():
    m = merged(**{SRC_UK: [full("B8", "UK", Child="new"), full("B9", "UK", Child="new")]})
    d = diff(m.rows, [full("B8", "UK"), full("b8 ", "UK"), full("B9", "UK")])
    assert [r.key for r in d.skipped_duplicates] == [("UK", "B8")]
    assert [w.key for w, _, _ in d.updates] == [("UK", "B9")]
    assert not d.inserts


def test_normalise_sort():
    assert normalise("Sort", "3.0") == 3 and normalise("Sort", "") is None and normalise("Sort", "x") == "x"


def test_season_is_carried_from_every_source():
    m = merged(**{SRC_US: [full("B1", "US", Season="Summer")],
                  SRC_GR: [{**full("B2", "DE", Season="Winter")}],
                  SRC_NON_US: [non_us(1, "B1", "CA", season="Monsoon"), non_us(2, "B3", "IT", season="All Season"),
                               non_us(3, "B2", "DE", season="")]})
    assert m.rows[("US", "B1")].values["Season"] == "Summer"
    assert m.rows[("CA", "B1")].values["Season"] == "Monsoon"      # non_us overrides when not blank
    assert m.rows[("DE", "B2")].values["Season"] == "Winter"       # blank non_us season does not override
    assert m.rows[("IT", "B3")].values["Season"] == "All Season"   # new non_us row
