"""Phase-scoped acceptance with explicit provider/host evidence and no skip success."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
REQUIRED={f"AC{i:02d}" for i in range(1,21)}


FEEDBACK_REQUIRED = {f"AC{i:02d}" for i in (*range(21, 29), *range(49, 55), *range(56, 59), 61, 65, 66)}

def read_manifest(path, phase=1, increment=None):
    document = json.loads(Path(path).read_text())
    if increment is not None:
        if increment != "feedback":
            raise ValueError("unsupported acceptance increment")
        document = document.get("increments", {}).get(increment, {})
        required, label = FEEDBACK_REQUIRED, "Feedback"
        valid_scope = document.get("increment") == increment
    else:
        if phase not in {1, 3}:
            raise ValueError("unsupported acceptance phase")
        if phase != 1:
            document = document.get("phases", {}).get(str(phase), {})
        required = REQUIRED if phase == 1 else {f"AC{i:02d}" for i in range(29, 37)}
        label, valid_scope = f"Phase {phase}", document.get("phase") == phase
    records = document.get("acceptance", {})
    if document.get("schema_version") != 1 or not valid_scope or set(records) != required:
        raise ValueError(f"{label} must map every declared shipping acceptance criterion")
    for record in records.values():
        if not isinstance(record, dict) or not record.get("offline"):
            raise ValueError("each acceptance requires nonempty executable offline evidence")
        for level in ("offline", "provider", "host", "review"):
            values = record.get(level, [])
            if not isinstance(values, list) or any(not isinstance(s, str) or not s for s in values):
                raise ValueError("invalid acceptance evidence selectors")
    return document


def flatten(suite):
    for item in suite:
        if isinstance(item,unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def selected_tests(selectors):
    selected={}
    loader=unittest.TestLoader()
    for selector in selectors:
        suite=loader.loadTestsFromName(selector)
        cases=list(flatten(suite))
        if not cases or any("._FailedTest." in case.id() for case in cases):
            raise ValueError("missing or empty test selector: "+selector)
        for case in cases:
            selected[case.id()]=case
    if not selected:
        raise ValueError("zero-test success is forbidden")
    return selected


class EvidenceResult(unittest.TextTestResult):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.outcomes={}

    def addSuccess(self,test):
        super().addSuccess(test)
        self.outcomes[test.id()]="passed"

    def addFailure(self,test,error):
        super().addFailure(test,error)
        self.outcomes[test.id()]="failed"

    def addError(self,test,error):
        super().addError(test,error)
        self.outcomes[test.id()]="error"

    def addSkip(self,test,reason):
        super().addSkip(test,reason)
        self.outcomes[test.id()]="skipped"

    def addSubTest(self,test,subtest,error):
        super().addSubTest(test,subtest,error)
        if error is not None:
            self.outcomes[test.id()]="failed"

    def wasSuccessful(self):
        return super().wasSuccessful() and not self.skipped


def _version(package):
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def validate_runtime():
    configured = os.environ.get("VIRTUAL_ENV")
    if (not configured or not Path(configured).is_absolute()
            or Path(sys.prefix).resolve() != Path(configured).resolve()):
        raise ValueError("Use plain python in the configured Hermes VIRTUAL_ENV/PATH; private environments are not supported")

def source_digest(*, synthesis=False):
    """Bind reused observations to implementation, not unrelated Markdown edits."""
    import hashlib
    if synthesis:
        paths = list((ROOT / "yandex_speech").glob("tts*.py"))
        paths += [ROOT / "yandex_speech/config.py", ROOT / "scripts/tts.py"]
        paths += list((ROOT / "integrations/hermes").glob("*.py"))
        paths.append(ROOT / "tests/provider/test_voice_quality.py")
    else:
        paths = [p for directory in ("yandex_speech", "scripts", "integrations", "tests")
                 for p in (ROOT / directory).rglob("*.py")]
        paths += list(ROOT.glob("requirements*.txt")) + [ROOT / "pyproject.toml"]
    paths.append(ROOT / "VERSION")
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        digest.update(str(path.relative_to(ROOT)).encode() + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def read_reviews(path, required):
    """Check an identified listener's artifact/source binding, not acoustic quality."""
    import hashlib
    import math
    if path is None:
        return {}
    path = Path(path)
    document = json.loads(path.read_text())
    if not isinstance(document, dict):
        raise ValueError("review evidence must be a mapping")
    records = document.get("reviews")
    if (document.get("schema_version") != 1 or document.get("source_digest") != source_digest(synthesis=True)
            or not isinstance(records, dict) or set(records) - set(required)):
        raise ValueError("review evidence has stale sources or unknown review identifiers")
    outcomes = {}
    for name, record in records.items():
        if (not isinstance(record, dict) or record.get("verdict") not in {"passed", "failed"}
                or not isinstance(record.get("reviewer"), str) or not record["reviewer"].strip()
                or not isinstance(record.get("note"), str) or not record["note"].strip()
                or not isinstance(record.get("artifacts"), list) or not record["artifacts"]):
            raise ValueError("review evidence needs an explicit verdict, identified listener, note and heard artifacts")
        if any(not isinstance(item, dict) for item in record["artifacts"]):
            raise ValueError("review artifacts must be mappings")
        if name == "voice-quality":
            from tests.provider.test_voice_quality import comparison_cases
            expected = {case["id"] for case in comparison_cases()}
            if (len(record["artifacts"]) != len(expected)
                    or {item.get("case_id") for item in record["artifacts"]} != expected
                    or len({item.get("path") for item in record["artifacts"]}) != len(expected)):
                raise ValueError("voice quality needs every labeled comparison, not one heard clip")
            if any(item.get("verdict") not in {"passed", "failed"} for item in record["artifacts"]):
                raise ValueError("every voice comparison needs its own listener verdict")
            if record["verdict"] == "passed" and any(item["verdict"] != "passed" for item in record["artifacts"]):
                raise ValueError("an overall voice pass cannot hide a failed comparison")
        if name == "telegram-delivery" and (record.get("route") != "telegram"
                or record.get("delivered") is not True or record.get("heard") is not True):
            raise ValueError("Telegram acceptance needs addressed delivery and actual listening confirmation")
        if name == "telegram-delivery":
            timings = record.get("timings")
            if (not isinstance(timings, dict)
                    or any(type(timings.get(field)) not in (int, float)
                           or not math.isfinite(timings[field]) or timings[field] < 0
                           for field in ("synthesis_seconds", "delivery_seconds"))
                    or not isinstance(record.get("timing_basis"), str) or not record["timing_basis"].strip()):
                raise ValueError("Telegram evidence needs separate observed synthesis and delivery timings")
        for artifact in record["artifacts"]:
            if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
                raise ValueError("review artifact is invalid")
            relative = Path(artifact["path"])
            target = path.parent / relative
            if (relative.is_absolute() or ".." in relative.parts or target.is_symlink()
                    or target.suffix.lower() not in {".mp3", ".wav", ".ogg", ".opus", ".flac"}
                    or not target.is_file() or not target.resolve().is_relative_to(path.parent.resolve())):
                raise ValueError("review artifact must be retained beside its receipt")
            if hashlib.sha256(target.read_bytes()).hexdigest() != artifact.get("sha256"):
                raise ValueError("reviewed audio differs from the retained artifact")
        outcomes[name] = record["verdict"]
    return outcomes


def read_provider_evidence(path, digest, hermes_state, packages):
    document = json.loads(Path(path).read_text())
    if (not isinstance(document, dict) or not isinstance(document.get("hermes"), dict)
            or not isinstance(hermes_state, dict)):
        raise ValueError("provider evidence must include its observed runtime")
    if (document.get("schema_version") != 1 or document.get("profile") != "grpc"
            or document.get("provider_opt_in") is not True
            or document.get("source_digest") != digest
            or document.get("hermes", {}).get("revision") != hermes_state.get("revision")
            or document["hermes"].get("worktree_clean") is not True
            or hermes_state.get("worktree_clean") is not True
            or document.get("python") != platform.python_version()
            or document.get("packages") != packages
            or type(document.get("test_count")) is not int or document["test_count"] <= 0
            or not isinstance(document.get("outcomes"), dict)
            or not document["outcomes"] or document.get("source_changed_during_run")):
        raise ValueError("provider evidence does not match clean source, Python, packages and explicit executed run")
    if any(value not in {"passed", "failed", "error", "skipped"} for value in document["outcomes"].values()):
        raise ValueError("provider evidence contains an unknown outcome")
    return document


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--phase", type=int, choices=[1, 3])
    scope.add_argument("--increment", choices=["feedback"])
    parser.add_argument("--profile", choices=["base", "grpc", "storage"], default="grpc")
    provider = parser.add_mutually_exclusive_group()
    provider.add_argument("--provider", action="store_true", help="Run billable synthetic provider checks")
    provider.add_argument("--provider-evidence", type=Path, help="Reuse a matching retained provider/host run without rebilling")
    parser.add_argument("--host", action="store_true")
    parser.add_argument("--maximum-recording", action="store_true",
                        help="Explicitly permit the >4-hour synthetic uploaded-file provider check")
    parser.add_argument("--review-evidence", type=Path, help="Retained listener verdicts with heard audio and source binding")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args(argv)
    if args.host and not args.provider:
        parser.error("--host requires --provider and explicit synthetic credentials/fixtures")
    if (args.provider or args.provider_evidence) and args.profile != "grpc":
        parser.error("provider/host evidence uses the grpc profile")
    if args.maximum_recording and not args.provider:
        parser.error("--maximum-recording requires --provider")
    return args

def main(argv=None):
    args = parse_args(argv)
    try:
        validate_runtime()
    except ValueError as error:
        raise SystemExit(str(error))
    os.environ["YANDEX_SPEECH_TEST_PROFILE"] = args.profile
    os.environ["YANDEX_SPEECH_LIVE_TESTS"] = "1" if args.provider else "0"
    os.environ["YANDEX_SPEECH_HOST_TESTS"] = "1" if args.host else "0"
    os.environ["YANDEX_SPEECH_MAXIMUM_RECORDING_TESTS"] = "1" if args.maximum_recording else "0"
    digest = source_digest()
    packages = {p: _version(p) for p in (
        "requests", "yandexcloud", "grpcio", "grpcio-tools", "boto3", "protobuf", "build")}
    hermes = {"revision": subprocess.run(["git", "-C", "/opt/hermes/src", "rev-parse", "HEAD"],
                  capture_output=True, text=True).stdout.strip(),
              "tag": subprocess.run(["git", "-C", "/opt/hermes/src", "describe", "--tags", "--exact-match"],
                  capture_output=True, text=True).stdout.strip()}
    native_status = subprocess.run(["git", "-C", "/opt/hermes/src", "status", "--porcelain"], capture_output=True, text=True)
    hermes["worktree_clean"] = native_status.returncode == 0 and not native_status.stdout.strip()
    if args.provider and not hermes["worktree_clean"]:
        print("Provider acceptance requires the unchanged stock host source", file=sys.stderr)
        return 1
    label = args.increment or f"phase{args.phase}"
    destination = args.evidence or ROOT / ".agent/tasks/public-release/testing-evidence" / (label + "-" + uuid.uuid4().hex + ".json")
    if destination.exists() or destination.is_symlink():
        print("Existing evidence is preserved; choose a fresh destination", file=sys.stderr)
        return 1
    try:
        manifest = read_manifest(ROOT / "tests/acceptance.json", phase=args.phase or 1, increment=args.increment)
        omitted = {"test_grpc_file", "test_job_recovery", "test_lifecycle", "test_stream_api",
                   "test_streaming", "test_timeline", "test_hermes", "test_tts_grpc",
                   "test_tts_stream", "test_partition_grpc"} if args.profile != "grpc" else set()
        if args.profile != "storage":
            omitted.add("test_storage_sdk")
        coverage, review_ids = {}, set()
        for ac, record in manifest["acceptance"].items():
            coverage[ac] = {}
            for level in ("offline", "provider", "host"):
                selectors = record.get(level, [])
                available = [s for s in selectors if level != "offline" or s.split(".")[1] not in omitted]
                pending = ["not-run:" + s for s in selectors if s not in available]
                coverage[ac][level] = (list(selected_tests(available)) if available else []) + pending
            review_ids.update(record.get("review", []))
            coverage[ac]["review"] = ["review:" + name for name in record.get("review", [])]
        reviews = read_reviews(args.review_evidence, review_ids)
        reused = (read_provider_evidence(args.provider_evidence, digest, hermes, packages)
                  if args.provider_evidence else None)
        modules = [f"tests.{p.stem}" for p in sorted((ROOT / "tests").glob("test_*.py")) if p.stem not in omitted]
        cases = selected_tests(modules)
        for record in manifest["acceptance"].values():
            for level, enabled in (("provider", args.provider), ("host", args.host)):
                if enabled and record.get(level):
                    cases.update(selected_tests(record[level]))
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        artifact = destination.open("x", encoding="utf-8")
        destination.chmod(0o600)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    started = time.monotonic()
    try:
        result = unittest.TextTestRunner(verbosity=1, buffer=True, resultclass=EvidenceResult).run(
            unittest.TestSuite(cases.values()))
        outcomes, reused_ids = dict(result.outcomes), set()
        if reused:
            for levels in coverage.values():
                for level in ("provider", "host"):
                    if level == "host" and reused.get("host_opt_in") is not True:
                        continue
                    for case in levels[level]:
                        if case not in outcomes and case in reused["outcomes"]:
                            outcomes[case] = reused["outcomes"][case]
                            reused_ids.add(case)
        outcomes.update({"review:" + name: outcome for name, outcome in reviews.items()})
        statuses = {}
        for ac, levels in coverage.items():
            required = {case for ids in levels.values() for case in ids}
            values = [outcomes.get(case, "not-run") for case in required]
            statuses[ac] = ("passed" if values and all(value == "passed" for value in values)
                else "failed" if any(value in {"failed", "error", "skipped"} for value in values)
                else "pending-required-evidence")
        changed = source_digest() != digest
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True)
        record = {"schema_version": 1, "phase": args.phase, "increment": args.increment, "profile": args.profile,
            "scope": manifest["scope"], "revision": revision.stdout.strip(),
            "source_digest": digest, "source_changed_during_run": changed,
            "tracked_state_dirty": bool(dirty.stdout.strip()), "interpreter": sys.executable,
            "python": platform.python_version(), "hermes": hermes, "packages": packages,
            "provider_opt_in": args.provider, "host_opt_in": args.host,
            "maximum_recording_opt_in": args.maximum_recording,
            "provider_evidence_reused": bool(reused), "reused_provider_test_count": len(reused_ids),
            "reviews": reviews, "elapsed_seconds": time.monotonic() - started,
            "test_count": result.testsRun, "outcomes": outcomes, "acceptance": statuses}
        json.dump(record, artifact, indent=2)
        artifact.flush()
        os.fsync(artifact.fileno())
    finally:
        artifact.close()
    passed = sum(value == "passed" for value in statuses.values())
    print(f"{result.testsRun} tests executed; {len(reused_ids)} provider/host cases reused; "
          f"{passed}/{len(statuses)} acceptance criteria complete; evidence: {destination}")
    pending = [ac for ac, value in statuses.items() if value != "passed"]
    if pending:
        print("Required evidence remains pending or failed: " + ", ".join(sorted(pending)))
    if changed:
        print("Sources changed during the run; acceptance must be revalidated", file=sys.stderr)
    if changed or not result.wasSuccessful() or any(value == "failed" for value in statuses.values()):
        return 1
    full = args.provider and args.host or bool(args.provider_evidence)
    return 0 if not full or passed == len(statuses) else 1


if __name__ == "__main__":
    raise SystemExit(main())
