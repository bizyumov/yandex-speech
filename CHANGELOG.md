# Changelog

All notable changes are recorded here. VERSION is the sole release-version source; package/CLI/plugin projections must agree. No historical release is inferred from commits.

## Unreleased

- Fix #81: automatic recording recognition admits inline audio before using configured S3; reserve optional uploads for explicit inline-size failures, preserve explicit storage/recovery, and document provider limits separately from client wire guards.

- Add S3 bucket/prefix setup with skill defaults, higher-priority env coordinates, scoped access probes and bucket creation via static credentials; keep endpoint in skill configuration instead of env.

- Default account-key responses to generated files in `~/secrets`, retain no-overwrite behavior and ordinary filesystem permissions, and add separate offline `apply-key` env application with exact credential mappings, shared folder-ID preservation and gateway startup guidance.

## 2026.11.9 - 2026-11-09

- Maintain the complete voice catalog with conditional API/model/language/role availability and a refresh procedure; generate optional previews on request. Use quoted installed-path examples with YS.

- Add a profile-scoped `yandex_speech_ssml` plugin tool for XML pronunciation on stock Hermes, with live read-only preference resolution, owned Opus output and explicit final-response media delivery instructions; keep the built-in plain-text tool unchanged.

- Add explicit read-only account/key inspection and setup planning, plus scoped API/static-key creation into exclusive protected files; keep grant/expiry evidence separate from actual method execution and never retry uncertain creation. Correct readiness to report delivered recording partition support.

- Add explicit v3 inline recording partitions with exact full-wire admission, complete sample coverage, durable parent/child recovery without rebilling accepted audio, timed overlap reconciliation and visible partition-local speaker identities; make size-limit errors actionable through storage/split CLI commands.

- Surface native streaming failures during audio feeding or preview; preserve the first safe cause through cleanup/cancellation, keep finalization nonraising and prevent cancellation races from committing success.

- Route recorded auto requests through capability-compatible gRPC/REST/raw-text fallback, retain pre-acceptance reasons, stop after accepted/uncertain work, and keep CLI recovery pointers specific to actual asynchronous jobs.

- Add SDK-independent REST v3 recording recognition, explicit durable v2/S3, version-preserving recovery across gRPC/REST, exact JSON-wire admission and genuine metadata enforcement; prepare WAV as headerless PCM for v2 and scope native storage credentials to the owning Hermes profile.

- Add public owned-upload receipts, conditional no-clobber S3 creation, metadata-based uncertain-upload recovery and explicit exact-version cleanup after durable recognition; retain unversioned sources safely.

- Recognize split recordings through exact 30-second PCM windows with five-second overlap; retain adjacent-boundary alternatives, cut-word evidence and legitimate repeated speech without invented timing.

- Drain workers registered during concurrent stream cancellation/finalization before closing worker admission; retain the existing shutdown deadline and genuine timeout errors.

- Remove the cancelled skill-owned Hermes configuration writer and enrollment switches. Use stock Hermes plugin admission/configuration CLI; verify installed-wheel STT/TTS registration, repeatability and profile/credential isolation without platform patches. Standalone readiness diagnostics remain available.

- Register paired native Hermes STT/TTS providers with profile-scoped defaults/secrets, per-call synthesis clients, protected actual output paths and eligible 24 kHz PCM streaming.

- Preserve native pronunciation controls, stressed words and internal pause context across file/duration/streaming request boundaries; reject oversized indivisible controls.
- Add opt-in synthetic evidence for server/default and external EOU, word timing, literary refinement correlation and final drain; measure FULL_DATA separately.

- Add synthesis API v3 REST/gRPC complete files, explicit LiveTTS model/voices, direct MP3, WAV length finalization, structural v1 SSML, safe long-text assembly and incremental 24 kHz gRPC PCM.

- Establish the development baseline: MIT, skill documentation, optional dependency profiles, HOME/workspace configuration and acceptance coverage.
- Add gRPC v3 recording recognition with private job receipts, restart recovery, complete result drain, original-media timing and two-speaker exports.
- Add bounded arriving-audio preparation and external-EOU completion, cancellation, previews and a native Hermes 0.23.0 STT provider.
- Preserve explicit REST and synthesis entry points; correct service-account folder handling, raw/literary provenance, wrapper defaults and output preservation.

Live meeting recording and additional Discord conversation modes remain next-release work.

## 0.1.0 - 2026-11-09

Published with an incorrect version identifier. Superseded by 2026.11.9 under the owner's YYYY.M.D[-N] calendar-versioning policy; the original tag is retained.
