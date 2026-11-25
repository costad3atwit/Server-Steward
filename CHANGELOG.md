# Changelog

## 0.1.0

First release.

- **Discord admin assistant**: owner-only DMs, tool calls validated against a schema generated from `steward.yaml`, ✅/❌ confirmation for disruptive actions, results formatted in code, audit logs.
- **Built-in actions**: server status, container logs (redacted), ordered stack restarts with health waits and retries, scheduled-action list, config reload.
- **Integrations** (off by default): Immich (job status, pause/resume with automatic resume, storage per user), Nextcloud (storage per user), Jellyfin and Audiobookshelf (who's streaming), Transmission (download progress), Gluetun (VPN status), backup log (last result and last success).
- **Help page assistant**: answers only from `knowledge/help`, no tools, cited sources, URL grounding, `UNSURE` hand-off, per-client rate limits, cold-start status endpoint, plain contact-form relay to Discord, example static page.
- **Knowledge search** over Markdown with a local embedding model; index rebuilt automatically when files change.
- **Packaging**: Docker Compose with an optional bundled Ollama, NVIDIA GPU override, and a local override for app networks.
