# Cold presence status settings ownership

`clover_cli.settings_projection.read_presence_status_settings()` owns the cold
presence-status read, not arbitrary behavioral configuration. `/emotions` must
work before startup and must not import the ordinary config module: that module
performs provider/plugin discovery and may back up malformed configuration.
Even its readonly loader cannot satisfy the no-runtime-import contract.

The configuration package, not the agent, owns file decoding and projection.
`agent.emotions_config.snapshot()` delegates to this entrypoint for compatibility.
The guard permits only this named module; other agent readers remain forbidden.
This is an explicit specialized owner, not permission to add raw settings reads.

Contract:
- Resolve the active profile with `get_clover_home()`; no hardcoded home.
- Read user settings with BOM support; absent/null means empty. Reject malformed
  mappings and dangling links without backup, migration or directory creation.
- Project only presence/proactive control and route leaves; normalize root-model
  aliases through the existing pure model normalizer. Never return arbitrary raw
  settings or secrets.
- Apply current strict managed settings per leaf above user settings. Do not use
  the forgiving startup cache or treat malformed policy as unrestricted.
- Reject interpolation on projected leaves instead of opening `.env`, auth stores
  or runtime providers. This owner deliberately does not promise ordinary config
  defaults or environment expansion.

Normal behavioral consumers still use `clover_cli.config.load_config()` or
`load_config_readonly()`. Raw write-back and diagnostics still use the existing
canonical raw primitives. Cold tests exercise real imports, synthetic profile
files, policy precedence, normalization and zero writes/secret/network access.
The guard's unauthorized-reader regression proves the new seam grants no other
module a raw-read exemption.
