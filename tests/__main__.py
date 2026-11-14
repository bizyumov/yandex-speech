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
ISSUE83_REQUIRED = {f"AC83-{i:02d}" for i in range(1, 12)}

def read_manifest(path, phase=1, increment=None):
    document = json.loads(Path(path).read_text())
    if increment is not None:
        if increment not in {"feedback", "issue83"}:
            raise ValueError("unsupported acceptance increment")
        document = document.get("increments", {}).get(increment, {})
        required, label = (ISSUE83_REQUIRED, "Issue83") if increment == "issue83" else (FEEDBACK_REQUIRED, "Feedback")
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
        paths += [ROOT / name for name in ("tests/issue83_cases.py",
            "tests/provider/issue83_fixtures.py", "tests/provider/test_issue83_native.py",
            "tests/__main__.py", "tests/acceptance.json")]
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



def retained_review_path(root, value, suffixes):
    if not isinstance(value, str):
        raise ValueError("retained review path must be a string")
    relative = Path(value)
    target = Path(root) / relative
    if (relative.is_absolute() or ".." in relative.parts or target.is_symlink()
            or target.suffix.lower() not in suffixes or not target.is_file()
            or not target.resolve().is_relative_to(Path(root).resolve())):
        raise ValueError("review evidence must be retained beside its receipt")
    return target


def native_review_cases(root, references, expected):
    """Join listening labels to successful current-source native generation."""
    import hashlib
    from tests.issue83_cases import native_cases, alena_cases, rollover_cases, automatic_cases
    scopes = {
        "issue83-native-matrix": {case["id"] for case in native_cases() + alena_cases() + rollover_cases()},
        "issue83-native-automatic": {case["id"] for case in automatic_cases()},
    }
    if not isinstance(references, list) or not references:
        raise ValueError("native listening needs its generation receipts")
    actual, seen_receipts = {}, set()
    for reference in references:
        if not isinstance(reference, dict):
            raise ValueError("native receipt reference must be a mapping")
        path = retained_review_path(root, reference.get("path"), {".json"})
        if path.resolve() in seen_receipts:
            raise ValueError("duplicate native generation receipt")
        seen_receipts.add(path.resolve())
        if hashlib.sha256(path.read_bytes()).hexdigest() != reference.get("sha256"):
            raise ValueError("native receipt hash differs")
        document = json.loads(path.read_text())
        if (document.get("schema_version") != 1 or document.get("source_digest") != source_digest()
                or document.get("synthesis_source_digest") != source_digest(synthesis=True)
                or document.get("sources_unchanged") is not True
                or document.get("credentials_unchanged") is not True
                or document.get("rollback", {}).get("restored") is not True
                or document.get("all_generation_delivery_cases_passed") is not True):
            raise ValueError("native generation is stale, incomplete or not restored")
        rows = document.get("cases")
        declared = document.get("expected_case_ids")
        scope_expected = scopes.get(document.get("scope"))
        if (not isinstance(rows, list) or not isinstance(declared, list) or scope_expected is None
                or len(rows) != len(scope_expected) or len(declared) != len(scope_expected)
                or set(declared) != scope_expected
                or any(not isinstance(row, dict) for row in rows)
                or {row.get("id") for row in rows} != scope_expected):
            raise ValueError("native generation receipt omits or repeats required cases")
        for row in rows:
            identity = row["id"]
            if identity in actual or row.get("status") != "passed-native-generation-delivery":
                raise ValueError("native comparison is repeated or failed")
            audio = retained_review_path(path.parent, row.get("delivered_file"),
                                         {".mp3", ".wav", ".ogg", ".opus", ".flac"})
            if hashlib.sha256(audio.read_bytes()).hexdigest() != row.get("sha256"):
                raise ValueError("native delivered audio differs from its generation receipt")
            actual[identity] = {"path": audio, "sha256": row["sha256"]}
    if set(actual) != set(expected):
        raise ValueError("native listening scope does not match generated comparisons")
    return actual


def native_tail_pcm(path, seconds):
    """QA-only deterministic excerpt; no decoder dependency is added to synthesis."""
    result = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-sseof", "-" + str(seconds),
        "-i", str(path), "-t", str(seconds), "-ar", "48000", "-ac", "1",
        "-f", "s16le", "pipe:1"], capture_output=True, timeout=30)
    if result.returncode or not result.stdout or len(result.stdout) % 2:
        raise ValueError("native listening excerpt cannot be decoded")
    return result.stdout


def verify_native_montage(root, montage, artifacts, native):
    import hashlib
    import math
    import wave
    if not isinstance(montage, dict):
        raise ValueError("listening montage must be a mapping")
    seconds, gap = montage.get("tail_seconds"), montage.get("gap_seconds")
    if (type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= 30
            or type(gap) not in (int, float) or not math.isfinite(gap) or not 0 <= gap <= 3):
        raise ValueError("listening excerpt/gap duration is invalid")
    path = retained_review_path(root, montage.get("path"), {".wav"})
    if hashlib.sha256(path.read_bytes()).hexdigest() != montage.get("sha256"):
        raise ValueError("listening montage hash differs")
    expected = hashlib.sha256()
    silence = b"\0\0" * round(48000 * gap)
    for index, artifact in enumerate(artifacts):
        if index:
            expected.update(silence)
        expected.update(native_tail_pcm(native[artifact["case_id"]]["path"], seconds))
    actual = hashlib.sha256()
    with wave.open(str(path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 48000):
            raise ValueError("listening montage PCM format differs")
        while data := audio.readframes(65536):
            actual.update(data)
    if actual.digest() != expected.digest():
        raise ValueError("listening montage does not contain the native comparison excerpts")

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
        native = None
        if name in {"voice-quality", "issue83-native-quality", "issue83-explicit-quality"}:
            if name in {"issue83-native-quality", "issue83-explicit-quality"}:
                from tests.issue83_cases import comparison_case_ids, native_cases, alena_cases, rollover_cases
                expected = (comparison_case_ids() if name == "issue83-native-quality" else
                    {case["id"] for case in native_cases() + alena_cases() + rollover_cases()})
                native = native_review_cases(path.parent, record.get("native_receipts"), expected)
            else:
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
            if native is not None and artifact.get("sha256") != native[artifact["case_id"]]["sha256"]:
                raise ValueError("heard artifact is not the native-generated comparison")
        if native is not None and record.get("montage") is not None:
            verify_native_montage(path.parent, record["montage"], record["artifacts"], native)
        outcomes[name] = record["verdict"]
    return outcomes


def read_provider_evidence(path, digest, hermes_state, packages, *,
                           allow_unchanged_core=False, eligible_core_cases=()):
    import hashlib
    raw = Path(path).read_bytes()
    document = json.loads(raw)
    if (not isinstance(document, dict) or not isinstance(document.get("hermes"), dict)
            or not isinstance(hermes_state, dict)):
        raise ValueError("provider evidence must include its observed runtime")
    if (document.get("schema_version") != 1 or document.get("profile") != "grpc"
            or document.get("provider_opt_in") is not True
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
    if document.get("source_digest") != digest:
        if not allow_unchanged_core:
            raise ValueError("provider evidence source digest does not match")
        from tests.evidence_compat import unchanged_core_evidence
        outcomes, proof = unchanged_core_evidence(document, ROOT, set(eligible_core_cases))
        document = dict(document, outcomes=outcomes, host_opt_in=False, reuse_proof=proof)
    document["evidence_source"] = {
        "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
        "revision": document.get("revision"), "source_digest": document.get("source_digest"),
        "reuse_proof": document.get("reuse_proof"),
        "host_evidence_eligible": document.get("host_opt_in") is True,
    }
    return document


def merge_provider_observations(documents):
    """Reject conflicting retained verdicts; current executed outcomes still take precedence."""
    outcomes, host_cases = {}, set()
    for document in documents:
        for case, outcome in document["outcomes"].items():
            if case in outcomes and outcomes[case] != outcome:
                raise ValueError("retained evidence has conflicting verdicts for " + case)
            outcomes[case] = outcome
            if document.get("host_opt_in") is True:
                host_cases.add(case)
    return outcomes, host_cases

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--phase", type=int, choices=[1, 3])
    scope.add_argument("--increment", choices=["feedback", "issue83"])
    parser.add_argument("--profile", choices=["base", "grpc", "storage"], default="grpc")
    provider = parser.add_mutually_exclusive_group()
    provider.add_argument("--provider", action="store_true", help="Run billable synthetic provider checks")
    provider.add_argument("--provider-evidence", type=Path, action="append",
                          help="Reuse matching retained runs; repeat for distinct provider/host observations")
    parser.add_argument("--reuse-unchanged-core", action="store_true",
                        help="After adapter review, reuse verified unchanged standalone core cases; never changed host cases")
    parser.add_argument("--host", action="store_true")
    parser.add_argument("--live-only", action="store_true",
                        help="Collect opted-in provider/host observations as the profile owner; offline evidence remains pending")
    parser.add_argument("--maximum-recording", action="store_true",
                        help="Explicitly permit the >4-hour synthetic uploaded-file provider check")
    parser.add_argument("--review-evidence", type=Path, help="Retained listener verdicts with heard audio and source binding")
    parser.add_argument("--validate-explicit-review", action="store_true",
                        help="Validate only the pre-auto issue83 listening gate; does not commission the increment")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args(argv)
    if args.validate_explicit_review and (args.increment != "issue83" or not args.review_evidence
            or args.provider or args.provider_evidence or args.host or args.live_only):
        parser.error("--validate-explicit-review requires issue83 and --review-evidence without live execution")
    if args.reuse_unchanged_core and not args.provider_evidence:
        parser.error("--reuse-unchanged-core requires retained provider evidence")
    if args.live_only and not args.provider:
        parser.error("--live-only requires --provider; it does not certify offline coverage")
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
    if args.validate_explicit_review:
        try:
            result = read_reviews(args.review_evidence, {"issue83-explicit-quality"})
        except (OSError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 1
        passed = result.get("issue83-explicit-quality") == "passed"
        print("Explicit-route listening " + ("validated" if passed else "not passed") +
              "; automatic routing and final commissioning remain pending")
        return 0 if passed else 1
    os.environ["YANDEX_SPEECH_TEST_PROFILE"] = args.profile
    os.environ["YANDEX_SPEECH_LIVE_TESTS"] = "1" if args.provider else "0"
    os.environ["YANDEX_SPEECH_HOST_TESTS"] = "1" if args.host else "0"
    os.environ["YANDEX_SPEECH_MAXIMUM_RECORDING_TESTS"] = "1" if args.maximum_recording else "0"
    digest = source_digest()
    packages = {p: _version(p) for p in (
        "requests", "yandexcloud", "grpcio", "grpcio-tools", "boto3", "protobuf", "build")}
    # The canonical stock dependency is root-owned; consumer-profile checks trust only
    # this installed source for read-only Git metadata, without changing actor configuration.
    hermes = {"revision": subprocess.run(["git", "-c", "safe.directory=/opt/hermes/src", "-C", "/opt/hermes/src", "rev-parse", "HEAD"],
                  capture_output=True, text=True).stdout.strip(),
              "tag": subprocess.run(["git", "-c", "safe.directory=/opt/hermes/src", "-C", "/opt/hermes/src", "describe", "--tags", "--exact-match"],
                  capture_output=True, text=True).stdout.strip()}
    native_status = subprocess.run(["git", "-c", "safe.directory=/opt/hermes/src", "-C", "/opt/hermes/src", "status", "--porcelain"], capture_output=True, text=True)
    hermes["worktree_clean"] = native_status.returncode == 0 and not native_status.stdout.strip()
    if args.provider and not hermes["worktree_clean"]:
        print("Provider acceptance requires the unchanged stock host source", file=sys.stderr)
        return 1
    label = args.increment or f"phase{args.phase}"
    evidence_root = ROOT / (".agent/tasks/issue-83/testing-evidence" if args.increment == "issue83"
                            else ".agent/tasks/public-release/testing-evidence")
    destination = args.evidence or evidence_root / (label + "-" + uuid.uuid4().hex + ".json")
    if destination.exists() or destination.is_symlink():
        print("Existing evidence is preserved; choose a fresh destination", file=sys.stderr)
        return 1
    try:
        manifest = read_manifest(ROOT / "tests/acceptance.json", phase=args.phase or 1, increment=args.increment)
        omitted = {"test_grpc_file", "test_job_recovery", "test_lifecycle", "test_stream_api",
                   "test_streaming", "test_timeline", "test_hermes", "test_tts_grpc",
                   "test_tts_stream", "test_tts_stream_file_grpc", "test_partition_grpc"} if args.profile != "grpc" else set()
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
        core_cases = {case for levels in coverage.values() for case in levels["provider"]}
        host_cases = {case for levels in coverage.values() for case in levels["host"]}
        reused = [read_provider_evidence(path, digest, hermes, packages,
                    allow_unchanged_core=args.reuse_unchanged_core,
                    eligible_core_cases=core_cases - host_cases)
                  for path in (args.provider_evidence or [])]
        retained_outcomes, retained_host_cases = merge_provider_observations(reused)
        modules = [] if args.live_only else [f"tests.{p.stem}" for p in sorted((ROOT / "tests").glob("test_*.py")) if p.stem not in omitted]
        cases = selected_tests(modules) if modules else {}
        for record in manifest["acceptance"].values():
            for level, enabled in (("provider", args.provider), ("host", args.host)):
                if enabled and record.get(level):
                    cases.update(selected_tests(record[level]))
        if not cases:
            raise ValueError("zero-test success is forbidden")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        artifact = destination.open("x", encoding="utf-8")
        destination.chmod(0o600)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    started = time.monotonic()
    try:
        result = unittest.TextTestRunner(verbosity=1, buffer=True, resultclass=EvidenceResult,
            failfast=args.host).run(
            unittest.TestSuite(cases.values()))
        outcomes, reused_ids = dict(result.outcomes), set()
        if reused:
            for levels in coverage.values():
                for level in ("provider", "host"):
                    for case in levels[level]:
                        if level == "host" and case not in retained_host_cases:
                            continue
                        if case not in outcomes and case in retained_outcomes:
                            outcomes[case] = retained_outcomes[case]
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
            "provider_opt_in": args.provider, "host_opt_in": args.host, "live_only": args.live_only,
            "maximum_recording_opt_in": args.maximum_recording,
            "provider_evidence_reused": bool(reused), "reused_provider_test_count": len(reused_ids),
            "provider_evidence_sources": [item["evidence_source"] for item in reused],
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
