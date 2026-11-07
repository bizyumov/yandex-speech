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


def read_manifest(path, phase=1):
    document=json.loads(Path(path).read_text())
    if phase not in {1,3}:
        raise ValueError("unsupported acceptance phase")
    if phase != 1:
        document=document.get("phases",{}).get(str(phase),{})
    records=document.get("acceptance",{})
    required=REQUIRED if phase == 1 else {f"AC{i:02d}" for i in range(29,37)}
    if document.get("schema_version") != 1 or document.get("phase") != phase or set(records) != required:
        raise ValueError(f"Phase {phase} must map every declared acceptance criterion")
    for record in records.values():
        if not record.get("offline"):
            raise ValueError("each acceptance requires nonempty executable offline evidence")
        for level in ["offline","provider","host"]:
            values=record.get(level,[])
            if not isinstance(values,list) or any(not isinstance(s,str) or not s for s in values):
                raise ValueError("invalid acceptance test selectors")
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

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase",type=int,choices=[1,3],required=True)
    parser.add_argument("--profile",choices=["base","grpc","storage"],default="grpc")
    parser.add_argument("--provider",action="store_true")
    parser.add_argument("--host",action="store_true")
    parser.add_argument("--evidence",type=Path)
    args=parser.parse_args(argv)
    if args.host and not args.provider:
        parser.error("--host requires --provider and explicit synthetic credentials/fixtures")
    if args.provider and args.profile != "grpc":
        parser.error("full provider/host acceptance uses the grpc profile")
    try:
        validate_runtime()
    except ValueError as error:
        parser.error(str(error))
    os.environ["YANDEX_SPEECH_TEST_PROFILE"]=args.profile
    # Flags grant this test process permission to call providers; ordinary CI cannot
    # inherit a live opt-in accidentally.
    os.environ["YANDEX_SPEECH_LIVE_TESTS"]="1" if args.provider else "0"
    os.environ["YANDEX_SPEECH_HOST_TESTS"]="1" if args.host else "0"
    try:
        manifest=read_manifest(ROOT/"tests/acceptance.json",phase=args.phase)
        coverage={}
        omitted={"test_grpc_file","test_job_recovery","test_lifecycle","test_stream_api",
                     "test_streaming","test_timeline","test_hermes","test_tts_grpc","test_tts_stream","test_partition_grpc"} if args.profile != "grpc" else set()
        if args.profile != "storage":
            omitted.add("test_storage_sdk")
        for ac,record in manifest["acceptance"].items():
            coverage[ac]={}
            for level,selectors in record.items():
                available=[s for s in selectors if level != "offline" or s.split(".")[1] not in omitted]
                pending=["not-run:"+s for s in selectors if s not in available]
                coverage[ac][level]=(list(selected_tests(available)) if available else [])+pending
        modules=[f"tests.{p.stem}" for p in sorted((ROOT/"tests").glob("test_*.py"))]
        modules=[name for name in modules if name.rsplit(".",1)[-1] not in omitted]
        cases=selected_tests(modules)
        for record in manifest["acceptance"].values():
            for level,enabled in [("provider",args.provider),("host",args.host)]:
                if enabled:
                    cases.update(selected_tests(record.get(level,[])) if record.get(level) else {})
    except (OSError,ValueError) as error:
        parser.error(str(error))
    started=time.monotonic()
    result=unittest.TextTestRunner(verbosity=1,buffer=True,resultclass=EvidenceResult).run(
        unittest.TestSuite(cases.values()))
    revision=subprocess.run(["git","rev-parse","HEAD"],cwd=ROOT,capture_output=True,text=True)
    dirty=subprocess.run(["git","status","--porcelain"],cwd=ROOT,capture_output=True,text=True)
    statuses={}
    for ac,levels in coverage.items():
        required_ids={case for ids in levels.values() for case in ids}
        outcomes=[result.outcomes.get(case,"not-run") for case in required_ids]
        if outcomes and all(value == "passed" for value in outcomes):
            statuses[ac]="passed"
        elif any(value in {"failed","error","skipped"} for value in outcomes):
            statuses[ac]="failed"
        else:
            statuses[ac]="pending-required-evidence"
    record={"schema_version":1,"phase":args.phase,"profile":args.profile,
        "revision":revision.stdout.strip(),"tracked_state_dirty":bool(dirty.stdout.strip()),
        "interpreter":sys.executable,"python":platform.python_version(),
        "hermes":{"revision":subprocess.run(["git","-C","/opt/hermes/src","rev-parse","HEAD"],capture_output=True,text=True).stdout.strip(),
                  "tag":subprocess.run(["git","-C","/opt/hermes/src","describe","--tags","--exact-match"],capture_output=True,text=True).stdout.strip()},
        "packages":{p:_version(p) for p in [
            "requests","yandexcloud","grpcio","grpcio-tools","boto3","protobuf","build"]},
        "provider_opt_in":args.provider,"host_opt_in":args.host,
        "elapsed_seconds":time.monotonic()-started,"test_count":result.testsRun,
        "outcomes":result.outcomes,"acceptance":statuses}
    destination=args.evidence or ROOT/".agent/tasks/public-release/testing-evidence"/(f"phase{args.phase}-"+uuid.uuid4().hex+".json")
    destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    try:
        with destination.open("x",encoding="utf-8") as stream:
            json.dump(record,stream,indent=2)
        destination.chmod(0o600)
    except OSError:
        print("Cannot create fresh evidence artifact; existing evidence is preserved",file=sys.stderr)
        return 1
    passed=sum(s == "passed" for s in statuses.values())
    print(f"{result.testsRun} tests; {passed}/{len(statuses)} acceptance criteria complete; evidence: {destination}")
    if not args.provider or not args.host:
        print("Required provider/host evidence remains pending for this offline/profile run.")
    if not result.wasSuccessful():
        return 1
    return 0 if not(args.provider and args.host) or passed == len(statuses) else 1


if __name__ == "__main__":
    raise SystemExit(main())
