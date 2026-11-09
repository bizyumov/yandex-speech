"""Conservative reuse of observed standalone core checks after adapter-only changes."""
import hashlib
import re
import subprocess
import tomllib
from pathlib import Path

CORE_PROVIDER_MODULES = (
    "tests.provider.test_recording.", "tests.provider.test_recording_routes.",
    "tests.provider.test_recording_maximum.", "tests.provider.test_rest_recording.",
    "tests.provider.test_overlap.", "tests.provider.test_owned_storage.",
)


def _source_path(name):
    return (name.endswith(".py") and name.split("/")[0] in {
        "yandex_speech", "scripts", "integrations", "tests"}) or (
        "/" not in name and (name in {"pyproject.toml", "VERSION"} or
                              name.startswith("requirements") and name.endswith(".txt")))


def _revision_sources(root, revision):
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("core reuse requires an exact original Git revision")
    try:
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-tree", "-r", "--name-only", "-z", revision],
            capture_output=True, check=True).stdout.decode().split("\0")
        return {name: subprocess.run(
            ["git", "-C", str(root), "cat-file", "blob", revision + ":" + name],
            capture_output=True, check=True).stdout
            for name in listed if name and _source_path(name)}
    except (OSError, UnicodeError, subprocess.CalledProcessError) as error:
        raise ValueError("original source revision cannot be verified locally") from error


def _project_compatible(old, new):
    before, after = tomllib.loads(old.decode()), tomllib.loads(new.decode())
    old_files = before.get("tool", {}).get("setuptools", {}).pop("data-files", {})
    new_files = after.get("tool", {}).get("setuptools", {}).pop("data-files", {})
    if before != after or old_files.keys() != new_files.keys():
        return False
    for destination, originals in old_files.items():
        current = new_files[destination]
        if not isinstance(current, list) or not set(originals) <= set(current):
            return False
        names = [Path(name).name for name in current]
        if len(names) != len(set(names)):
            return False
        added = set(current) - set(originals)
        if added and (not destination.startswith("share/yandex-speech/integrations/")
                      or any(not name.startswith("integrations/") or not name.endswith(".py")
                             for name in added)):
            return False
    return True


def unchanged_core_evidence(document, root, eligible_cases):
    """Return provider-only observations and their proof; never reuse changed host checks.

    The caller explicitly opts into this component boundary after reviewing the adapter
    change. The original whole-source digest must reconstruct exactly from its Git revision.
    """
    root = Path(root).resolve()
    old = _revision_sources(root, document.get("revision"))
    digest = hashlib.sha256()
    for name in sorted(old, key=Path):
        raw = old[name]
        digest.update(name.encode() + b"\0")
        digest.update(raw)
    if digest.hexdigest() != document.get("source_digest"):
        raise ValueError("original source digest does not match its retained Git revision")
    dependencies = {name for name in old if "/" not in name and name.startswith("requirements") and name.endswith(".txt")}
    if {path.name for path in root.glob("requirements*.txt")} != dependencies:
        raise ValueError("dependency input set changed; fresh provider evidence is required")
    checked = []
    for name, raw in old.items():
        if (name.startswith(("yandex_speech/", "scripts/", "tests/provider/"))
                or name == "tests/__init__.py" or "/" not in name):
            path = root / name
            if path.is_symlink() or not path.is_file() or path.resolve() != root / name:
                raise ValueError("a retained core input is missing or redirected: " + name)
            current = path.read_bytes()
            equal = _project_compatible(raw, current) if name == "pyproject.toml" else current == raw
            if not equal:
                raise ValueError("a retained core implementation or fixture changed: " + name)
            checked.append(name)
    for directory in ("yandex_speech", "scripts"):
        if any(str(path.relative_to(root)) not in old for path in (root / directory).rglob("*.py")):
            raise ValueError("a new core module requires fresh provider evidence")
    for path in (root / "tests/provider").rglob("__init__.py"):
        shadowed = str(path.parent.with_suffix(".py").relative_to(root))
        if str(path.relative_to(root)) not in old and shadowed in old:
            raise ValueError("a new fixture package shadows an original producer module")
    outcomes = {case: value for case, value in document["outcomes"].items()
                if case in eligible_cases and case.startswith(CORE_PROVIDER_MODULES)}
    if not outcomes:
        raise ValueError("no verified standalone core cases cover the current requirements")
    return outcomes, {
        "kind": "reviewed-unchanged-standalone-core", "origin_revision": document["revision"],
        "origin_source_digest": document["source_digest"], "checked_files": sorted(checked),
        "host_evidence_reused": False,
    }
