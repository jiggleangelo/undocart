"""Remedy: deterministic, returns-first remedy engine."""

from .artefacts import generate
from .classify import classify
from .models import Case, Decision
from .sample_data import SAMPLE_CASES, make_sources
from . import selfserve as _selfserve
from . import store as _store
from .review_fixes import install_selfserve_fixes, install_store_fixes
from .store_contract_fixes import install as install_store_contract_fixes

install_selfserve_fixes(_selfserve)
install_store_fixes(_store)
install_store_contract_fixes(_store)

__all__ = ["Case", "Decision", "SAMPLE_CASES", "classify", "generate", "make_sources"]
