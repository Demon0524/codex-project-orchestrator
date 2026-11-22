# Third-party code and provenance

This release selectively integrates two MIT-licensed upstream projects. It does not embed their complete runtimes or claim their client compatibility.

## codex-app-server-mcp

- Repository: https://github.com/j-pollack/codex-app-server-mcp
- Pinned commit: `237b2a4455f16e1325e4f5328d40afb9be6ba62f`
- Original: `src/app-server-client.ts`
- Local adaptation: `server/rpc_client.py` ports bounded abandoned-request/late-response handling, strict JSONL dispatch and process lifecycle patterns to Python. Local Codex discovery, proxy selection and fail-closed approval behavior remain project-specific.
- License: [MIT, Copyright (c) 2026 Jonathan](licenses/codex-app-server-mcp-MIT.txt).

## session-handoff

- Repository: https://github.com/yuzushi-dev/session-handoff
- Pinned commit: `6201b9028080b65a75f950e17ca4ac225749dcef`
- Original: `server/handoff_state.py`
- Local adaptation: `server/vendor/session_handoff_state.py` reuses structured state validation and Markdown rendering, removing the dependency on the upstream MCP service/redaction import.
- Related design reference: `server/session_switch.py`; this release uses a local persistent transition journal and immutable checkpoints, not the upstream managed CLI launcher, session migration, telemetry, or supervisor installation.
- License: [MIT, Copyright (c) 2026 Daniele Verì](licenses/session-handoff-MIT.txt).

See source headers for modifications. Both complete license texts are distributed with source and release archives.
