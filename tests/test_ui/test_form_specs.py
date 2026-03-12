"""Tests that FieldSpec definitions align with Pydantic config models.

Every non-``enabled`` field on a registered config model must be covered
by either a FieldSpec in ``FORM_SPECS`` or an entry in ``DEFERRED_FIELDS``.
This catches drift when new fields are added to a model but not the UI.
"""

from __future__ import annotations

import pytest

from oasisagent.db.registry import (
    CONNECTOR_TYPES,
    CORE_SERVICE_TYPES,
    NOTIFICATION_TYPES,
)
from oasisagent.ui.form_specs import (
    DEFERRED_FIELDS,
    FORM_SPECS,
    SINGLE_INSTANCE_TYPES,
    TYPE_DESCRIPTIONS,
    TYPE_DISPLAY_NAMES,
    FieldSpec,
    get_description,
    get_display_name,
    get_form_specs,
)

ALL_TYPES = {**CONNECTOR_TYPES, **CORE_SERVICE_TYPES, **NOTIFICATION_TYPES}


class TestFieldSpecAlignment:
    """Every model field (except ``enabled``) has a FieldSpec or is deferred."""

    @pytest.mark.parametrize("type_name", sorted(ALL_TYPES))
    def test_all_fields_covered(self, type_name: str) -> None:
        model = ALL_TYPES[type_name].model
        model_fields = set(model.model_fields.keys()) - {"enabled"}
        spec_fields = {s.name for s in FORM_SPECS.get(type_name, [])}
        deferred = set(DEFERRED_FIELDS.get(type_name, []))
        covered = spec_fields | deferred

        missing = model_fields - covered
        assert not missing, (
            f"{type_name}: model fields not covered by FieldSpec or "
            f"DEFERRED_FIELDS: {missing}"
        )

    @pytest.mark.parametrize("type_name", sorted(ALL_TYPES))
    def test_no_extra_specs(self, type_name: str) -> None:
        """FieldSpec names must correspond to actual model fields."""
        model = ALL_TYPES[type_name].model
        model_fields = set(model.model_fields.keys())
        spec_fields = {s.name for s in FORM_SPECS.get(type_name, [])}
        extra = spec_fields - model_fields
        assert not extra, (
            f"{type_name}: FieldSpec names not on model: {extra}"
        )

    @pytest.mark.parametrize("type_name", sorted(ALL_TYPES))
    def test_enabled_excluded(self, type_name: str) -> None:
        """``enabled`` must never appear in FieldSpecs."""
        spec_names = {s.name for s in FORM_SPECS.get(type_name, [])}
        assert "enabled" not in spec_names, (
            f"{type_name}: 'enabled' must not be in FORM_SPECS"
        )


class TestFormSpecsRegistry:
    """FORM_SPECS covers all registered types and metadata is present."""

    @pytest.mark.parametrize("type_name", sorted(ALL_TYPES))
    def test_form_specs_key_exists(self, type_name: str) -> None:
        assert type_name in FORM_SPECS, (
            f"Missing FORM_SPECS entry for registered type '{type_name}'"
        )

    @pytest.mark.parametrize("type_name", sorted(ALL_TYPES))
    def test_display_name_exists(self, type_name: str) -> None:
        name = get_display_name(type_name)
        assert name != type_name, (
            f"Missing TYPE_DISPLAY_NAMES entry for '{type_name}'"
        )

    @pytest.mark.parametrize("type_name", sorted(ALL_TYPES))
    def test_description_exists(self, type_name: str) -> None:
        desc = get_description(type_name)
        assert desc, f"Missing TYPE_DESCRIPTIONS entry for '{type_name}'"

    def test_no_orphan_form_specs(self) -> None:
        """FORM_SPECS should not have keys for unregistered types."""
        orphans = set(FORM_SPECS) - set(ALL_TYPES)
        assert not orphans, f"Orphan FORM_SPECS entries: {orphans}"

    def test_no_orphan_display_names(self) -> None:
        orphans = set(TYPE_DISPLAY_NAMES) - set(ALL_TYPES)
        assert not orphans, f"Orphan TYPE_DISPLAY_NAMES entries: {orphans}"

    def test_no_orphan_descriptions(self) -> None:
        orphans = set(TYPE_DESCRIPTIONS) - set(ALL_TYPES)
        assert not orphans, f"Orphan TYPE_DESCRIPTIONS entries: {orphans}"


class TestFieldSpecDataclass:
    """FieldSpec dataclass behaves correctly."""

    def test_defaults(self) -> None:
        spec = FieldSpec("host", "Host", "text")
        assert spec.name == "host"
        assert spec.label == "Host"
        assert spec.input_type == "text"
        assert spec.required is False
        assert spec.default is None
        assert spec.show_when is None
        assert spec.min_val is None
        assert spec.max_val is None

    def test_all_params(self) -> None:
        spec = FieldSpec(
            "port", "Port", "number",
            required=True,
            default=8080,
            help_text="Server port",
            group="Connection",
            min_val=1,
            max_val=65535,
        )
        assert spec.required is True
        assert spec.default == 8080
        assert spec.min_val == 1
        assert spec.max_val == 65535

    def test_select_options(self) -> None:
        spec = FieldSpec(
            "mode", "Mode", "select",
            options=[("a", "Option A"), ("b", "Option B")],
        )
        assert spec.options is not None
        assert len(spec.options) == 2

    @pytest.mark.parametrize("type_name", sorted(ALL_TYPES))
    def test_secret_fields_are_password_inputs(self, type_name: str) -> None:
        """Fields listed in TypeMeta.secret_fields should use password input."""
        meta = ALL_TYPES[type_name]
        secret_fields = set(meta.secret_fields or [])
        for spec in FORM_SPECS.get(type_name, []):
            if spec.name in secret_fields:
                assert spec.input_type == "password", (
                    f"{type_name}.{spec.name} is a secret field "
                    f"but uses input_type='{spec.input_type}' instead of 'password'"
                )


class TestSingleInstanceTypes:
    """SINGLE_INSTANCE_TYPES is a valid frozenset of registered types."""

    def test_all_single_instance_types_registered(self) -> None:
        unregistered = SINGLE_INSTANCE_TYPES - set(ALL_TYPES)
        assert not unregistered, (
            f"SINGLE_INSTANCE_TYPES has unregistered types: {unregistered}"
        )


class TestHelperFunctions:
    def test_get_form_specs_unknown(self) -> None:
        assert get_form_specs("nonexistent") == []

    def test_get_display_name_unknown(self) -> None:
        assert get_display_name("nonexistent") == "nonexistent"

    def test_get_description_unknown(self) -> None:
        assert get_description("nonexistent") == ""
