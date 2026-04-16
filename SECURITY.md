# Security Policy

EchoVessel is a local-first daemon that talks to LLM providers, voice
providers, and (optionally) Discord. All persona memory and conversation
history live on the user's own machine. Even so, vulnerabilities in the
runtime, the Web UI, or the channel adapters could leak data, expose API
keys, or allow remote code execution against a user's local daemon.

If you believe you have found a security issue, please report it privately
before disclosing it publicly.

## Supported versions

The project is in early alpha. Only the most recent **0.0.x** release line
receives security fixes; older 0.0.x patches will not be backported.

| Version | Supported          |
| ------- | ------------------ |
| 0.0.x (latest) | ✅ |
| anything older | ❌ |

When the project moves to a 0.1 series, this matrix will be updated to keep
two lines (0.1.x and 0.0.x) supported during the transition window.

## Reporting a vulnerability

Please use **GitHub's private security advisory** channel:

> <https://github.com/AlanY1an/echovessel/security/advisories/new>

GitHub security advisories are visible only to the reporter and the repo
maintainers, and let us privately discuss, triage, and ship a fix before
public disclosure.

If GitHub is not an option for you, you may instead email the maintainer:

- **TBD — maintainer security email not yet published.**
  <!-- TODO: replace with a real contact address before the next public
       release. Until this is filled, the GitHub security advisory channel
       above is the only working private channel. -->

Please **do not** report security issues in public GitHub issues, in pull
requests, or on any chat channel. Public disclosure before a fix is shipped
puts every user of the affected version at risk.

## What to include in a report

A useful report typically contains:

1. A short description of the issue and its impact.
2. The version of EchoVessel you tested against (`echovessel --version` or
   the `version` field in `pyproject.toml`).
3. The platform (macOS / Linux distribution) and Python version.
4. Steps to reproduce — ideally a minimal config and command sequence.
5. Whether the issue requires a network attacker, a local attacker on the
   same machine, a malicious LLM/voice provider response, or a malicious
   Discord message.
6. Any suggested fix or mitigation, if you have one.

## Response SLA (best-effort)

This is a single-maintainer project; the SLAs below are aspirational, not
contractual.

| Stage | Target |
| ----- | ------ |
| Acknowledge receipt of report | within **3 business days** |
| Initial triage (confirm / reject / request more info) | within **7 days** |
| Patch released for a confirmed High / Critical issue | within **30 days** of confirmation |
| Public disclosure (CVE / security advisory) | coordinated with the reporter, typically after a fix has shipped |

If a report sits longer than these targets without a response, please feel
free to nudge the maintainer through the same private channel.

## Scope

In scope:

- The `echovessel` Python package and its CLI entry points.
- The bundled Web channel (FastAPI backend + React SPA bundle).
- The bundled Discord channel adapter.
- The `hatch_build.py` build hook and the `config.toml.sample` shipped in the wheel.

Out of scope:

- Vulnerabilities in upstream dependencies (FastAPI, uvicorn, anthropic,
  openai, discord.py, sentence-transformers, sqlite-vec, fish-audio-sdk,
  etc.). Please report those directly to the upstream maintainers.
- LLM provider behaviour itself (prompt injection that the model falls for,
  hallucinated content, biased output). EchoVessel passes user content
  through to the configured provider; defending against the provider's own
  failure modes is not in this project's scope.
- Issues that require the attacker to already have full local user
  privileges on the same machine running the daemon — at that point the
  attacker can read `~/.echovessel/` directly and the daemon offers no
  additional defence.

## Hardening notes for users

A few precautions every user can take:

- Keep `~/.echovessel/.env` mode `0600` (read/write owner only). The
  daemon does not enforce this, but a leaked `.env` is the most likely
  source of an API key incident.
- Bind the Web channel to `127.0.0.1` (the default) rather than `0.0.0.0`.
  The Web UI does not implement authentication; making it reachable from
  the network exposes the persona admin endpoints to any local user or
  remote attacker who can route to the port.
- Treat imported source material (chat logs, novels, transcripts) as
  untrusted input. The importer pipeline runs LLM extraction over user-
  supplied text; carefully crafted material could attempt prompt-injection
  attacks on the extractor. The `import_/` module is not yet wired into a
  production HTTP route in 0.0.1, but the same caution applies for any
  hand-rolled import script.
