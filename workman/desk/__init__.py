"""Workman Desk — cross-platform composer that drives local CLI seats."""
from .vendors import Vendor, build_argv, catalog, parse_grok_models

__all__ = ["Vendor", "build_argv", "catalog", "parse_grok_models"]
