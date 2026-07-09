"""Tests for the dataset_templates prompt-quality migration (0020).

Migration 0019 seeded a system default `dataset_templates` row whose
`collage_prompt` contained Handlebars templating that the Go backend does
NOT render (it is sent literally to the image model), plus instructions
that produced low-variety training data (constant neutral background and
expression). Migration 0020 fixes the prompt text and adds a second
system template tuned for Z-Image (ZIT) training.

These tests load the migration module directly by file path (it lives in
a numerically-prefixed file that isn't a valid Python package name) and
assert on the prompt content itself, without touching a real database.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "0020_dataset_templates_prompt_quality.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "migration_0020_dataset_templates_prompt_quality", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_module_imports():
    module = _load_migration_module()
    assert module.revision == "0020"
    assert module.down_revision == "0019"
    assert module.branch_labels is None
    assert module.depends_on is None
    assert hasattr(module, "upgrade")
    assert hasattr(module, "downgrade")


def test_improved_prompt_has_no_handlebars():
    module = _load_migration_module()
    prompt = module.IMPROVED_COLLAGE_PROMPT
    assert "{{#" not in prompt
    assert "{{@" not in prompt


def test_improved_prompt_has_variety_markers():
    module = _load_migration_module()
    prompt = module.IMPROVED_COLLAGE_PROMPT
    assert "DIFFERENT background" in prompt
    assert "FULL-BODY" in prompt
    assert "seamless 4x4 grid" in prompt
