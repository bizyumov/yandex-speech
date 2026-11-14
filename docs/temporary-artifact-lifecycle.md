# Specification: automatic disposal of temporary speech artifacts — #98

**Status: Specify — awaiting operator review.** Owner-requested addition to PR #97.
GitHub issue #98 is the requirements authority; this file is its complete review
projection. Existing #83 implementation approval remains effective for its existing
scope. This addition is not implemented or commissioned by publishing this spec.

## Objective and scope

After speech work stops needing its own temporary data, remove that data without
requiring the agent to remember a separate housekeeping step. Preserve originals,
final exports, other operations and data still needed by accepted cloud work.
Cover both standalone SDK/CLI and stock Hermes STT/TTS integration in PR #97.

Assumptions made explicit: automatic disposal is the normal lifecycle, not an
opt-in retention timer; only invocation-owned intermediates are disposable;
a local cancellation is not confirmation that a cloud job stopped. No arbitrary
age, scheduled sweeper, bucket-wide operation or Hermes source change is proposed.

## Inspected baseline

Application revision: 872fb98c3072ad7fdeec702d4ad7e2eadbe16dec.

- Upload receipts say `retention: retain`. Normal recognition completion does not
  call the existing uploader cleanup method.
- Existing cleanup requires a matching completed/no-speech job, durable saved
  result and exact immutable uploaded version. Keep these ownership protections.
- Local STT cancellation cancels local RPC/waiting. It does not establish remote
  cancellation. A local failed/cancelled status alone cannot authorize deletion.
- TTS retains partial parts after failure; the native provider can retain its
  own staging directory. Neither interface currently exposes a supported general
  recovery operation for those abandoned synthesis parts.
- Issue #98 already records a versioned synthetic-object live canary and explicit
  cleanup. That is prior evidence, not a new test of the proposed automatic path.

## Required lifecycle

| Observed operation state | Required disposition |
|---|---|
| Recognition result is durably saved and no other accepted consumer needs the upload | Automatically dispose of the exact owned upload version before returning the completed operation. Preserve the saved result and final exports. |
| Provider confirms terminal recognition failure/cancellation; no remaining consumer or supported recovery needs the input | Dispose of the owned upload; preserve its receipt and useful error diagnostics. |
| Recognition was definitely never submitted | Dispose of the operation's verified upload when the attempted operation ends. |
| Provider acceptance is uncertain, an accepted job is still active, or only the local wait was cancelled/timed out | Preserve the upload and durable job identity. Reconcile provider state by read/recovery before deciding disposal; never resubmit speech to obtain cleanup evidence. |
| TTS succeeds | Publish the complete final output first, then dispose of owned staging and intermediate parts. Returned final audio remains available for the caller/host to deliver. |
| TTS fails, is cancelled or its caller closes the producer | Stop owned producers and dispose of abandoned fragments/staging. Retain parts only while a real supported continuation within that operation needs them. Unreachable directories are not recovery. |
| Process exits abruptly | On the next invocation/recovery in the same owned state scope, reconcile registered artifact receipts. Skip active owners and uncertain cloud jobs; reclaim only proven eligible artifacts. No age-based inference. |
| Deletion is denied, fails or has an uncertain response | Persist `cleanup_pending` with the exact owned target and actionable cause. Provide disposal-only retry; never report the artifact deleted without verification. |

Unconditional failure-fragment retention in #83 is superseded by this owner-requested
addition only after its specification approval. This addition does not invent TTS
cloud operation IDs or a new paid synthesis recovery method.

## Ownership and recovery contract

1. Register ownership before creating/uploading an intermediate; preserve enough
   durable state to reconcile a crash between mutation and acknowledgement.
2. An owned S3 object is identified by its upload receipt, bucket, key, immutable
   version and verified ownership metadata. Do not delete caller-supplied URLs,
   originals, another version or an entire prefix. A second consumer must prevent
   disposal until it also releases the artifact.
3. Preserve the existing refusal to delete an unversioned or unverifiable object.
   Report the exact prerequisite as `cleanup_pending`; do not silently enable
   versioning, modify retention or discard the receipt. Recognition/result
   recovery must remain available without another paid recognition request.
4. Track local artifacts by their recorded operation ownership and active lease;
   a basename, directory prefix, PID alone or file age does not prove ownership.
   Preserve unrelated entries, externally replaced files and final output paths.
5. Disposal is idempotent. Resolve a lost delete reply by checking that exact
   owned version/path. Concurrent live owners and incomplete recovery must remain
   protected. An absent owned target can settle a pending deletion.
6. Expose disposal status on durable operation receipts and SDK/CLI diagnostics:
   deleted, retained-active, retained-uncertain, retained-recovery or cleanup-pending,
   with a reason and receipt reference. Do not log audio/text or credentials.
7. A cleanup failure must not hide the primary speech failure or invalidate an
   already saved speech result. Make the cleanup failure observable in the stock
   integration as well as standalone calls.
8. Provide a documented disposal-only retry addressed to a receipt. Its contract
   is read/reconcile/delete only: no recognition, synthesis, upload or account/key
   mutation. Exact implementation/API placement belongs to the next Plan phase.

## Stack, structure and style

Use existing Python >=3.11, standard-library file/locking/JSON facilities and the
existing optional boto3==1.42.89 storage profile. This change does not require a
new runtime, package installation, gRPC dependency or daemon.

Relevant existing code: `yandex_speech/storage.py`, `transcription.py`, `jobs.py`,
`artifacts.py`, `scripts/tts.py`, `tts_stream_file.py` and the stock providers in
`integrations/hermes/`. Tests belong in `tests/`; portable agent instructions
belong in `SKILL.md` and `references/`. Private execution evidence stays outside
published files. The Plan phase will choose the minimal file/change boundaries.

Follow existing snake_case APIs, `SpeechError` categories, durable receipt updates
and explicit ownership checks. An existing style example to preserve is:

```python
if not version or version == "null":
    raise SpeechError("PREREQUISITE", "safe cleanup requires an immutable uploaded object version")
```

Do not replace a useful error/receipt with a bare successful-looking return.

## Commands and verification

All execution uses the configured standard Hermes environment. Inside Hermes,
invoke plain `python`; outside it, retrieve the active configured environment and
verify `sys.prefix == VIRTUAL_ENV` before executing. No private/project venv.

```sh
python -m unittest discover -s tests -v
git diff --check
GITMARK_NO_GIT=1 gitmark --root . index
gitmark --root . lint
```

Implementation verification must answer these finite questions:

- Does a completed operation leave only the intended result, with its owned
  temporary object/staging removed and unrelated data preserved?
- Can active/uncertain work and concurrent consumers prevent premature deletion?
- Can failure, cancellation and process restart dispose of eligible artifacts
  without another paid speech submission?
- Is denied/uncertain cleanup visible and retryable while the original result
  or error remains usable?

Use deterministic local fixtures for failure, cancellation, concurrent ownership,
version replacement, missing permissions, lost delete replies and process restart.
Do not create a voices/transports/length cross-product for cleanup verification.
For commissioning, propose only two focused One-ring canaries: one synthetic
storage-backed recognition and one ordinary short native TTS reply. Each proves
result delivery plus the artifact disposition of that same operation. Disposal
retry must add zero paid speech submissions. The separate 65-case matrix remains
paused; these canaries are a proposed acceptance scope, not executed tests or
permission to alter live agent configuration.

## Acceptance criteria

- [ ] **AC98-01 — completed recognition:** after durable result publication, normal
  SDK/CLI/stock integration automatically deletes only the eligible owned uploaded
  version. Originals, saved transcripts, unrelated/newer versions and final exports
  survive. Inline recognition creates no cleanup upload.
- [ ] **AC98-02 — safe terminal disposition:** confirmed terminal provider failure,
  confirmed remote cancellation and definite pre-submission failure release their
  eligible uploads. Local cancellation/timeout, active accepted work and uncertain
  acceptance preserve the source until reconciled. Local status alone is insufficient.
- [ ] **AC98-03 — synthesis disposal:** successful file/stream work preserves the
  complete final output and removes its intermediates. Failure, cancellation and
  producer close remove abandoned parts/staging from standalone and native calls,
  including both `.yandex-speech-partial-*` and `.yandex-native-tts-*` ownership cases.
- [ ] **AC98-04 — restart and concurrency:** after process interruption the next
  owned recovery/invocation reconciles registered state, skips live owners/consumers
  and disposes of only eligible artifacts. No periodic or age-based sweep is used.
- [ ] **AC98-05 — retry and uncertainty:** denied/failed/uncertain deletion records
  cleanup-pending, remains actionable and retries idempotently without upload or
  paid speech replay. Exact-target absence can resolve a lost deletion response.
- [ ] **AC98-06 — protected data:** no original, caller URL, unrelated/replaced local
  file, other object version or final export is deleted. Unversioned/unverifiable
  targets stay pending with a precise prerequisite; no IAM/retention change occurs.
- [ ] **AC98-07 — observable integration:** durable receipts and SDK/CLI/native
  diagnostics expose disposition and cause without secrets or productive text/audio.
  Cleanup failure neither hides the speech error nor loses a saved successful result.
- [ ] **AC98-08 — bounded evidence:** local fault/restart/ownership checks pass in the
  standard Hermes runtime; the two focused One-ring canaries prove result delivery
  and disposal at the tested source revision before commissioning. Old explicit
  cleanup evidence, mocks alone and a merged PR are not commissioning evidence.

## Boundaries and approval

Always preserve ownership, active work, productive outputs, standard runtime and
stock Hermes interfaces. Ask first for live IAM/credential changes, bucket policy
changes, dependency/runtime changes or extra paid test dimensions. Never delete
by prefix/age, patch Hermes, resubmit accepted speech for cleanup, resume the held
65-case matrix or claim acoustic/commissioning acceptance from these checks.

No unresolved product question is delegated to the operator. Review is requested
on whether this specification faithfully and completely expresses #98. Approval
advances this addition to Plan, not implementation, commissioning or release.
