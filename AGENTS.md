# Yandex Speech development instructions

## Source-code tools

Serena MCP is MANDATORY for source-code reads and writes. Inspect its full method catalogue; use semantic lookup/reference methods and the appropriate source-edit methods. Do not bypass Serena with shell/Python source reads/writes, apply_patch or bulk source dumps. If a required tool is unavailable, reconcile managed tool availability before source editing.

Markdown is NOT source code. Use ordinary file tools for Markdown. Builds, tests and Git commands may run through shell tools.

## Required development runtime

ALL skill development, execution and tests MUST use the configured standard Hermes environment. Inside Hermes, run plain `python`: `VIRTUAL_ENV` and `PATH` are already configured. Hardcoded interpreter paths and commands that prepend a venv path are PROHIBITED. Project-local and other private development venvs remain prohibited. Outside Hermes, resolve the configured runtime/environment rather than assuming its location. Validate `sys.prefix` against the configured `VIRTUAL_ENV`; application child processes use `sys.executable`. Documentation and agent terminal examples must use plain `python`.

## Delivery workflow

GitHub Issues holds complete specifications, numbered acceptance criteria and dependencies. Group the current phase into a software increment, implement and test it, open a PR, resolve review findings and merge only when required checks pass. Preserve root SKILL.md as the skill entry and existing user interfaces. Keep private task intelligence and credentials out of published content.

## Markdown search and complete document indexing

Markdown MUST use GitMark indexing/search before locating or reading project documents. Index ALL project Markdown regardless of .gitignore, including ignored task specifications and instructions. Run `GITMARK_NO_GIT=1 gitmark --root /opt/skills/yandex-speech index`, then `gitmark --root /opt/skills/yandex-speech search "query"`; open only the relevant returned documents. Verify ignored documents are searchable. Reindex after Markdown edits and commits. Keep .gitmark derived/ignored and private task material unpublished. Markdown may be edited with ordinary file tools; Serena is mandatory for source code.


## External Hermes ownership boundary — operator correction, 2026-11-07

Hermes is an external upstream dependency. Speech-skill work does not authorize changing its source, adding platform interfaces, maintaining a fork or deploying an assistant-modified platform. Use stock Hermes plugin APIs, including its existing streaming factory. Do not make a custom expected-state writer or another Hermes source update a prerequisite for this plugin.

An explicit operator command to install a named official Hermes release authorizes that installation, not subsequent application feature patches. If a requested integration cannot be implemented through stock supported interfaces, establish the exact limitation and return it for a scope decision; do not silently expand into platform development. Preserve the operator's original functional requirements and state any unmet capability honestly.

## Release version policy — operator correction, 2026-11-09

Use YYYY.M.D[-N] calendar versions, with unpadded month/day and an optional same-day revision suffix. Use the approved release date in the operator's timezone. VERSION is the sole authority; CLI/package/plugin projections and the unprefixed release tag must agree. Do not choose semantic-version defaults. Before publishing, verify the requested release identity and existing tags; preserve already-published immutable tags when correcting an identifier.
