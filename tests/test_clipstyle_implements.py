from __future__ import annotations

import pytest

from app.stores import clipstyle_packages as cp

FILES = {"manifest.ts": "export const manifest = 1;\n"}


def _config(**extra):
    return {"manifest": {"id": "neonCard", "version": 1, "kind": "carousel"}, "files": FILES, **extra}


def test_a_package_without_implements_is_unchanged():
    cp.check_package_config(_config())


@pytest.mark.parametrize("value", ["technologyCarousel@1", "carousel@1", "abc@123456"])
def test_a_well_formed_implements_is_accepted(value):
    cp.check_package_config(_config(implements=value))


@pytest.mark.parametrize("value", ["", "technologyCarousel", "technologyCarousel@0", "Tech@1", "x@1", "a b@1", 7,
                                   {"contract": "carousel@1"}, "carousel@1234567", "neonCard@1"])
def test_a_malformed_or_self_implements_is_refused(value):
    with pytest.raises(ValueError, match="invalid_package_config:implements"):
        cp.check_package_config(_config(implements=value))
