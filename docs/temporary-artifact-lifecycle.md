# Spec: automatic S3 upload cleanup and abandoned TTS fragments (#98)

**Status: Specify — awaiting operator review.** Included in PR #97. Issue #98
is the requirements authority; this file is its complete review projection.

## Objective

Automatically delete **temporary audio objects uploaded by this skill to Yandex
Object Storage through its S3 API** once recognition no longer needs them. The
agent must not have to remember a separate S3 cleanup call after each transcript.
The original local recording and the saved transcript must remain available.

The primary scope is the S3 upload lifecycle in standalone SDK/CLI and stock
Hermes recognition. The secondary scope, already present in #98, is disposal of
abandoned local TTS fragments. Both belong in the existing PR #97.

Assumptions made explicit: automatic deletion follows operation state, without a
retention timer; only skill-owned temporary data is eligible; stopping a local
wait does not prove that an accepted cloud recognition job stopped.

At the inspected application baseline 872fb98, S3 upload receipts declare
`retention: retain`, and normal recognition completion does not invoke the existing
`ObjectStorageUploader.cleanup()`. That method already checks a matching completed
job, a durable saved result and an immutable S3 object version. This change adds
automatic lifecycle handling while preserving those ownership protections.

The required S3 behavior is:

| S3 upload / recognition condition | Required action |
|---|---|
| Inline recognition; no S3 upload was created | Perform no S3 upload or deletion for cleanup. |
| Skill-created S3 upload; cloud job accepted and still running | Keep the S3 object available to that job. |
| Recognition completed, transcript durably saved, no other consumer needs the upload | Automatically delete that exact owned S3 object version before returning completion; preserve the recording, saved result and exported transcript. |
| Provider confirms terminal failure or reports cancellation, with no remaining consumer or supported recovery needing the input | Delete the eligible owned S3 object version; preserve the job/upload receipt and useful error. |
| Upload succeeded but recognition was definitely never submitted | Delete the eligible owned S3 object version when that attempted operation ends. |
| Recognition acceptance is uncertain, or the local wait was cancelled/timed out | Keep the S3 object and job/upload receipts; reconcile the existing cloud job before disposal. Never submit recognition again merely to clean S3. |
| S3 PUT acknowledgement was lost | Recover the existing upload identity through its ownership receipt and S3 inspection; do not repeat PUT or recognition. Delete only after ownership, version and consumer state are established. |
| Process stopped before S3 cleanup finished | The next invocation/recovery in the same owned state scope reconciles the saved receipt and retries eligible deletion. Live owners and accepted/uncertain jobs stay protected. |
| S3 deletion is denied, fails or its response is lost | Persist cleanup-pending with the exact target and cause; verify/retry that same deletion without re-uploading audio or repeating paid speech work. |

Each S3 deletion must address the receipt's **bucket, key and immutable VersionId**
using the existing S3 client. Verify that exact version's ownership before deleting
it. Preserve unrelated objects and newer versions at the same key. A second active
consumer prevents deletion. A caller-supplied S3/HTTPS URL without the skill's
ownership receipt does not authorize deletion.

If the S3 object has no immutable version or ownership cannot be verified, retain
it with an explicit cleanup-pending prerequisite and an actionable receipt.
Do not silently enable bucket versioning, change retention, bypass a deletion
restriction or claim cleanup succeeded. Missing S3 inspection/deletion permission
must identify the failed operation without changing IAM or credentials.

S3 cleanup failure must neither hide a recognition failure nor invalidate a saved
successful transcript. Expose the cleanup outcome to SDK/CLI and the stock Hermes
integration. Provide receipt-addressed disposal-only retry: inspect/reconcile/delete,
with zero new speech submissions. Exact API placement belongs to Plan.

For the secondary TTS scope: after final audio publication, remove its owned staging
and intermediates while preserving the returned audio for delivery. On terminal
failure, cancellation or producer close, dispose of abandoned parts. Keep fragments
only while a real supported continuation still needs them. The current general
TTS interfaces do not expose a recovery operation for abandoned directories;
indefinite retention is not recovery. This proposed rule replaces unconditional
failed-part retention in #83 after approval. It creates no new TTS cloud-operation ID.

## Tech Stack

Python >=3.11; existing standard-library file, locking and JSON facilities;
existing optional S3 profile `boto3==1.42.89`. Reuse the existing S3 client,
credential resolution and ownership receipts. This change requires no new runtime,
package installation, daemon or gRPC dependency.

## Commands

Use the configured standard Hermes environment. Inside Hermes use plain `python`;
outside it retrieve that environment and verify `sys.prefix == VIRTUAL_ENV`.

```sh
python -m unittest discover -s tests -v
git diff --check
GITMARK_NO_GIT=1 gitmark --root . index
gitmark --root . lint
```

These are verification commands for subsequent implementation. No new cleanup CLI
command is claimed to exist at Specify; its executable interface belongs to Plan.

## Project Structure

```text
yandex_speech/storage.py       S3 upload receipts, exact-version inspection/deletion
yandex_speech/transcription.py Recognition submission, completion and recovery
yandex_speech/jobs.py          Durable recognition job and saved-result state
yandex_speech/artifacts.py     Existing local ownership/publication utilities
scripts/tts.py                Complete synthesis and owned intermediate parts
yandex_speech/tts_stream_file.py Streaming-file synthesis and partial output
integrations/hermes/          Stock STT/TTS provider integration
tests/                        Local lifecycle, ownership and fault tests
SKILL.md, references/          Agent instructions, S3 prerequisites and recovery
```

Keep private execution evidence outside published files. Choose the minimal
implementation boundaries during Plan rather than introducing another subsystem
or a competing task queue here.

## Code Style

Preserve existing snake_case APIs, `SpeechError` categories and durable receipt
updates. Keep S3 ownership checks explicit. The existing exact-version cleanup
style includes this check:

```python
if response.get("VersionId") != version:
    raise SpeechError("STATE", "storage did not identify the exact owned version")
```

A successful-looking return cannot replace a failed deletion or its receipt.
Diagnostics must describe the S3 action and disposition without audio, transcript
content or credentials.

## Testing Strategy

Use existing unittest fixtures in `tests/` and the standard Hermes runtime.
Test these S3 questions locally with deterministic storage/provider fixtures:

- Does normal completion remove only the uploaded S3 version after saving the
  transcript, while preserving the local original, other objects and newer versions?
- Do active consumers, uncertain acceptance and local cancellation prevent
  premature S3 deletion?
- Do definite rejection, terminal provider failure and process restart lead to
  eligible cleanup without another PUT or recognition submission?
- Can denied/uncertain S3 deletion be retried, including a lost response followed
  by confirmed absence, without losing the transcript or masking its error?

Local tests must also verify that recorded local ownership protects unrelated or
replaced files, and that final TTS output survives while abandoned staging is removed.
Use recorded ownership and a live ownership lease; names, prefixes, PID alone and
file age are insufficient evidence. No directory/prefix sweep is acceptance.

Proposed commissioning scope is two focused One-ring canaries: **one synthetic
S3-backed recognition**, proving delivered transcript plus deletion of its owned
S3 version, and **one short native TTS reply**, proving delivered audio plus local
staging disposal. Fault combinations stay local. Cleanup retry adds zero paid
speech submissions. The 65-case matrix remains paused. These canaries are proposed,
not executed tests or permission to modify live agent configuration.

## Boundaries

- Always: register ownership before creating/uploading intermediates; preserve
  enough durable state to reconcile a lost acknowledgement or process interruption;
  preserve originals, final outputs and active consumers; use stock Hermes interfaces
  and the standard runtime; report exact S3 cleanup disposition and receipt.
- Ask first: live IAM/credential, bucket-versioning/retention or runtime/dependency
  changes, and additional paid test dimensions.
- Never: delete caller-owned S3 sources, unrelated/newer versions or final exports;
  delete by prefix/age; resubmit paid speech for housekeeping; patch Hermes; resume
  the paused 65-case matrix; claim acoustic or commissioning acceptance from mocks.

## Success Criteria

- [ ] **AC98-01 — S3 after successful recognition:** normal SDK/CLI/stock integration
  automatically deletes the eligible owned S3 version after durable result saving.
  Local originals, saved/exported transcripts, unrelated S3 objects and newer versions
  survive. Inline recognition creates no S3 cleanup upload.
- [ ] **AC98-02 — S3 after failure/cancellation:** confirmed terminal provider failure,
  provider-reported cancellation and definite pre-submission failure release eligible
  S3 uploads. Accepted running jobs, uncertain acceptance and local cancellation/timeout
  preserve S3 input until reconciled. Local failed/cancelled status alone is insufficient.
- [ ] **AC98-03 — secondary TTS fragments:** successful synthesis preserves final audio
  and removes owned intermediates. Failure, cancellation and producer close dispose of
  abandoned standalone/native fragments, including the ownership cases represented by
  `.yandex-speech-partial-*` and `.yandex-native-tts-*`; no prefix sweep is allowed.
- [ ] **AC98-04 — S3/local restart and concurrency:** next owned invocation/recovery
  reconciles registered receipts after interruption, skips live owners/consumers and
  removes only eligible S3 versions/local intermediates. No periodic/age-based sweep.
- [ ] **AC98-05 — S3/local cleanup retry:** denied, failed or uncertain deletion is
  persisted as cleanup-pending and retried idempotently. Exact-target absence can
  settle an uncertain deletion. Retry performs no new upload or paid speech submission.
- [ ] **AC98-06 — S3/local protection:** bucket/key/VersionId and ownership checks prevent
  deletion of originals, caller URLs, unrelated/newer S3 versions, replaced local files
  and final exports. Unversioned/unverifiable S3 targets remain pending with a precise
  prerequisite; no IAM, versioning or retention mutation occurs.
- [ ] **AC98-07 — S3 diagnostics and result preservation:** durable receipts and SDK/CLI/
  native diagnostics expose deletion, retained-active, retained-uncertain,
  retained-recovery or cleanup-pending disposition, cause and recovery reference.
  Cleanup failure neither masks the speech error nor loses a successful saved result.
  No productive payload or credential is logged.
- [ ] **AC98-08 — bounded evidence:** local fault/restart/ownership tests pass; the focused
  One-ring S3 recognition proves delivered transcript plus owned-version removal, and
  the short native TTS canary proves delivered audio plus staging disposal at the tested
  revision. Prior explicit cleanup, mocks alone and merge do not prove automatic cleanup.

## Open Questions

No unresolved product choice is delegated to the operator. The review decision is
whether this S3-first specification faithfully and completely expresses #98 and its
secondary TTS requirement. Approval advances this addition to Plan, not implementation,
commissioning or release. Existing #83 authorization and its test pause remain unchanged.
