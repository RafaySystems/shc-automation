# utils/helpers.py -- replace the existing _PATCH_HOOK_FILES /
# load_canned_patch_commands() block with this.

import os
import re
from pathlib import Path

# Section names match UpgradeEngine's hook stages exactly -- no
# translation layer needed between this file's keys and the engine's
# constructor args.
_PATCH_SECTIONS = [
    "pre_commands",
    "config_patches",
    "after_radm_dependency",
    "after_radm_application",
    "post_commands",
    "after_radm_cluster",
]

_SECTION_RE = re.compile(r"^\[([a-z_]+)\]\s*$")


def _parse_patch_file(path: str) -> dict:
    """
    Parses a single [section]-delimited patch file into
    {"pre_commands": [...], "config_patches": [...], ...}.

    Comments (#) and blank lines are ignored everywhere. A command line
    found before any [section] header, or a section name that isn't one
    of _PATCH_SECTIONS, raises ValueError -- fail loudly on a malformed
    file rather than silently dropping commands.
    """
    result = {s: [] for s in _PATCH_SECTIONS}
    current = None
    with open(path) as f:
        for lineno, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            m = _SECTION_RE.match(line)
            if m:
                name = m.group(1)
                if name not in result:
                    raise ValueError(
                        f"{path}:{lineno}: unknown section [{name}] -- "
                        f"expected one of {_PATCH_SECTIONS}"
                    )
                current = name
                continue
            if current is None:
                raise ValueError(
                    f"{path}:{lineno}: command found before any [section] "
                    f"header: {line!r}"
                )
            result[current].append(line)
    return result


def load_canned_patch_commands(src_package_url: str, dst_package_url: str,
                                 src_version: str = None, dst_version: str = None,
                                 patches_root: str = None) -> dict:
    """
    Returns a dict with all six _PATCH_SECTIONS keys, each a list of
    command strings (possibly empty). Missing file -> all-empty dict,
    not an error -- most src->dst pairs won't need a canned patch at all.

    Lookup key preference:
      1. Explicit src_version/dst_version if given -- the normal path
         when Jenkins is driving (dropdown value passed straight
         through, never parsed). Produces clean filenames like
         "3.1-39__to__3.1-40-1.txt".
      2. Falls back to the package URL basename (extension stripped)
         when no explicit version is given -- keeps direct/manual
         pytest invocations (no Jenkins in front) working without
         requiring --src-version/--dst-version to be passed.
    """
    empty = {s: [] for s in _PATCH_SECTIONS}
    if not src_package_url or not dst_package_url:
        return empty

    if patches_root is None:
        patches_root = str(Path(__file__).parent.parent / "config" / "hops")

    key_src = src_version or src_package_url.rsplit("/", 1)[-1].replace(".tar.gz", "")
    key_dst = dst_version or dst_package_url.rsplit("/", 1)[-1].replace(".tar.gz", "")
    patch_file = os.path.join(patches_root, f"{key_src}__to__{key_dst}.txt")

    if not os.path.isfile(patch_file):
        return empty

    return _parse_patch_file(patch_file)