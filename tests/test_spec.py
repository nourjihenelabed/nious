"""Profile tests: the declared chain, and the grain rule that replaced allocation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pricing import profiles  # noqa: E402
from pricing.spec import (  # noqa: E402
    KEY,
    LINE,
    Dimension,
    Kind,
    Origin,
    Profile,
    Step,
    propagate_grain,
    validate_chain,
)


def level(name, origin=Origin.MEASURE, grain=LINE):
    """A level step, for building chains in tests."""
    return Step(name, Kind.LEVEL, origin, grain)


def deduction(name, origin=Origin.MEASURE, grain=LINE):
    """A deduction step, for building chains in tests."""
    return Step(name, Kind.DEDUCTION, origin, grain)


# -- chain validation --------------------------------------------------------


def test_a_chain_must_alternate_levels_and_deductions():
    """Two deductions in a row have no level between them to be a gap in."""
    problems = validate_chain((level("a"), deduction("b"), deduction("c"), level("d")))
    assert any("must alternate" in p for p in problems)


def test_a_chain_must_open_and_close_on_a_level():
    """A waterfall that starts on a deduction has nothing to deduct from."""
    assert any("must open on a level" in p for p in validate_chain((deduction("a"), level("b"))))
    assert any("must close on a level" in p for p in validate_chain((level("a"), deduction("b"))))


def test_a_derived_deduction_needs_a_measured_level_below_it():
    """Deriving each from the other is circular, and the only way to write one."""
    chain = (
        level("a"),
        deduction("b", Origin.DERIVED),
        level("c", Origin.DERIVED),
    )
    assert any("derived from each other" in p for p in validate_chain(chain))


def test_a_valid_chain_has_no_complaints():
    """The shipped shape must pass its own checker."""
    assert validate_chain(profiles.B2B_INDUSTRIAL.steps) == []
    assert validate_chain(profiles.DAIRY_AL_KHARJ.steps) == []


def test_an_unusable_chain_is_rejected_at_construction():
    """A malformed profile must fail at import, not produce a plausible number."""
    with pytest.raises(ValueError, match="unusable chain"):
        Profile(
            name="broken",
            entity="line",
            key="id",
            steps=(level("a"), deduction("b"), deduction("c"), level("d")),
            dimensions=(),
        )


# -- grain propagation -------------------------------------------------------


def test_grain_propagates_down_the_chain_from_the_coarsest_input():
    """Everything below a document-grain measure is document-grain too."""
    chain = propagate_grain(
        (
            level("list_value"),
            deduction("off_list", Origin.DERIVED),
            level("gross"),
            deduction("discount", grain=KEY),
            level("invoice", Origin.DERIVED),
            deduction("cogs"),
            level("margin", Origin.DERIVED),
        )
    )
    by_name = {s.name: s.grain for s in chain}
    assert by_name["off_list"] == LINE, "above the document-grain measure"
    assert by_name["invoice"] == KEY
    assert by_name["margin"] == KEY, "grain must not recover below a coarse step"


def test_the_shipped_profiles_have_the_grain_the_data_supports():
    """Order-grain discounts in one schema; everything at plan grain in the other."""
    b2b = profiles.B2B_INDUSTRIAL
    assert b2b.step("list_value").grain == LINE
    assert b2b.step("on_invoice_discount").grain == KEY
    assert b2b.step("pocket_margin").grain == KEY

    dairy = profiles.DAIRY_AL_KHARJ
    assert set(dairy.steps_at(KEY)) == set(), "the dairy plan is one row per key"


# -- key safety --------------------------------------------------------------


def test_customer_is_key_safe_and_product_is_not():
    """This distinction is what decides whether a step abstains."""
    b2b = profiles.B2B_INDUSTRIAL
    assert b2b.key_safe(["customer_id"])
    assert b2b.key_safe(["customer_id", "region_id"])
    assert not b2b.key_safe(["product_id"])
    assert not b2b.key_safe(["customer_id", "product_id"]), "one line-grain column is enough"


def test_an_unknown_dimension_is_assumed_line_grain():
    """Assuming otherwise is how an unallocatable number gets published."""
    assert not profiles.B2B_INDUSTRIAL.key_safe(["something_nobody_declared"])


def test_grouping_by_the_key_itself_is_always_safe():
    """One document per group cannot split a document."""
    assert profiles.B2B_INDUSTRIAL.key_safe(["transaction_id"])


# -- the registry ------------------------------------------------------------


def test_every_registered_profile_is_internally_consistent():
    """A profile whose closing level is not in its chain would break every caller."""
    for name, profile in profiles.REGISTRY.items():
        assert profile.name == name
        assert profile.has(profile.opening) and profile.has(profile.closing)
        assert profile.build_facts is not None, f"{name} declares no extraction"
        assert profile.tables, f"{name} names no tables"
        for dim in profile.dimensions:
            assert dim.grain in (LINE, KEY)


def test_absent_steps_are_explained_not_merely_missing():
    """A step the mapping cannot support must say why, for the contract to repeat."""
    dairy = profiles.DAIRY_AL_KHARJ
    assert "list_value" in dairy.absent_steps
    assert not dairy.has("list_value")
    for step, reason in dairy.absent_steps.items():
        assert not dairy.has(step), f"{step} is both declared and marked absent"
        assert len(reason) > 20, f"{step}'s reason is not an explanation"


def test_unknown_profile_names_are_rejected_with_the_known_ones():
    """A typo should print the alternatives, not fail somewhere downstream."""
    with pytest.raises(ValueError, match="Unknown pricing profile"):
        profiles.get_profile("dairy")


def test_a_host_project_can_register_its_own_profile():
    """Onboarding is adding a profile, not forking the engine."""
    custom = Profile(
        name="test_only",
        entity="line",
        key="id",
        steps=(level("revenue"), deduction("cost"), level("margin", Origin.DERIVED)),
        dimensions=(Dimension("product", "product_id", LINE),),
    )
    try:
        profiles.register(custom)
        assert profiles.get_profile("test_only") is custom
    finally:
        profiles.REGISTRY.pop("test_only", None)
