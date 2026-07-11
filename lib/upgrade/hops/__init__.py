"""
lib/upgrade/hops/

One file per upgrade hop, plus the loader logic to find and validate them
(get_hop / list_available_hops below) -- there is no separate
upgrade_registry.py anymore. This package is the single place that both
defines hops and knows how to find them.

Naming convention for hop files:

    upgrade_<from_short>_to_<to_short>.py

where <from_short>/<to_short> is the trailing numeric part of the version
string, e.g. version "3.1-40" -> short "40". So the 3.1-39 -> 3.1-40 hop
lives in:

    upgrade_39_to_40.py

Each hop file defines exactly one module-level dict named HOP. See
upgrade_39_to_40.py for the full template and an explanation of every
available hook key.

To add a new hop:
  1. Copy an existing upgrade_*.py file as a template.
  2. Rename it to match the new from/to versions.
  3. Update the "from"/"to" fields and the command lists.
  4. Nothing else needs to change -- get_hop() below discovers and
     validates the file automatically based on its filename + its declared
     "from"/"to" fields.

Usage from upgrade_engine.py:

    from lib.upgrade.hops import get_hop

    hop = get_hop("3.1-39", "3.1-40")
    hop["pre_commands"]            # list[str]
    hop["after_radm_dependency"]   # list[str]
    hop["after_radm_application"]  # list[str]
    hop["post_commands"]           # list[str]
    hop["after_radm_cluster"]      # list[str]
"""

import importlib
import pkgutil


# Every key an upgrade_engine.py caller might look for on a hop dict.
# Any key not defined in a given upgrade_*.py file is filled in as an
# empty list by get_hop() below, so hop files that only define a subset
# of hooks don't break the engine when it looks one up.
HOOK_KEYS = (
    "pre_commands",
    "after_radm_dependency",
    "after_radm_application",
    "post_commands",
    "after_radm_cluster",
)


def _short_version(version: str) -> str:
    """
    '3.1-39' -> '39'
    Takes the trailing numeric segment after the last '-', matching the
    upgrade_<from_short>_to_<to_short>.py filename convention.
    """
    return version.split("-")[-1]


def _expected_module_name(from_version: str, to_version: str) -> str:
    return f"upgrade_{_short_version(from_version)}_to_{_short_version(to_version)}"


def get_hop(from_version: str, to_version: str) -> dict:
    """
    Look up and load a hop from this package.

    Raises ValueError if:
      - no file matching the expected name exists for this from/to pair
      - the file exists but doesn't define a HOP dict
      - the file's declared from/to doesn't match what was requested
        (this catches short-name collisions -- e.g. two different full
        versions that happen to produce the same trailing short segment)
    """
    module_name = _expected_module_name(from_version, to_version)

    try:
        module = importlib.import_module(f"lib.upgrade.hops.{module_name}")
    except ModuleNotFoundError as e:
        raise ValueError(
            f"No upgrade path registered for {from_version} -> {to_version}.\n"
            f"Expected file: lib/upgrade/hops/{module_name}.py\n"
            f"Create it with a HOP dict -- see "
            f"lib/upgrade/hops/upgrade_39_to_40.py for the template."
        ) from e

    hop = getattr(module, "HOP", None)
    if hop is None:
        raise ValueError(
            f"lib/upgrade/hops/{module_name}.py must define a module-level "
            f"HOP dict (see upgrade_39_to_40.py for the template)."
        )

    declared_from = hop.get("from")
    declared_to   = hop.get("to")
    if declared_from != from_version or declared_to != to_version:
        raise ValueError(
            f"Version mismatch in lib/upgrade/hops/{module_name}.py: "
            f"file declares {declared_from!r} -> {declared_to!r}, but "
            f"{from_version!r} -> {to_version!r} was requested. This "
            f"usually means two different upgrades collided on the same "
            f"short filename (e.g. two versions both ending in the same "
            f"trailing number) -- check the 'from'/'to' fields in that file."
        )

    # Fill in any hook keys the file didn't define, so the engine can
    # always safely read hop[key] without a KeyError, regardless of how
    # many hooks a given hop file actually uses.
    for key in HOOK_KEYS:
        hop.setdefault(key, [])

    return hop


def list_available_hops() -> list:
    """
    Return the from/to pairs of every hop file currently present in this
    package. Useful for debugging / listing supported upgrade paths
    without hardcoding them anywhere.
    """
    import lib.upgrade.hops as _self

    pairs = []
    for _, module_name, _ in pkgutil.iter_modules(_self.__path__):
        if not module_name.startswith("upgrade_"):
            continue
        try:
            module = importlib.import_module(f"lib.upgrade.hops.{module_name}")
        except Exception:
            continue
        hop = getattr(module, "HOP", None)
        if hop and "from" in hop and "to" in hop:
            pairs.append((hop["from"], hop["to"]))
    return pairs