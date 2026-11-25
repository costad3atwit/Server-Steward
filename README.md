# Server Steward

A self-hosted AI assistant for your home server, running on your own hardware.

- **Discord admin assistant.** DM it in plain English ("how's the server doing?", "who's streaming?", "restart the media stack", "pause photo transcoding for 24 hours") and it answers or acts, using only the actions you allow. Anything disruptive waits for your ✅.
- **Help page assistant.** A question box for the people who use your server. It answers only from a FAQ you write, shows where each answer came from, and can't change anything.

Everything runs locally in Docker with a model served by [Ollama](https://ollama.com). No cloud APIs, no subscriptions, no open ports.


## How it stays safe

The language model never gets a shell, the Docker socket or your API keys. It can only *propose* one of a fixed set of actions; separate code checks it, you confirm it, and a small locked-down container carries it out.

```
 Discord DM ──▶ steward (agent) ──── verb + args ────▶ steward-executor ──▶ Docker, Immich, Jellyfin, ...
                 model picks an action   (token,           re-checks against your config,
                 you confirm with ✅      internal network)  runs only that action

 Help page ───▶ steward /help: answers from your FAQ only, no tools at all
```

- Only actions written into the executor exist, and only for the containers and limits in your config.
- Only your Discord account is listened to; everyone else is ignored and logged.
- The help path has no tools, so nothing typed into a public box can act on the server.
- Results are written by code, never by the model, so it can't misreport what happened.
- Everything is audited.

Details: [docs/security.md](docs/security.md).

## What it can do

| Ask | Needs ✅ | Integration |
| --- | --- | --- |
| How's the server doing? (containers, disk, memory, temperatures) | no | built in |
| Show me the last 30 lines of a container's logs (secrets redacted) | no | built in |
| Restart a stack, in order, waiting for each container to be healthy | yes | built in |
| Did last night's backup work? When was the last good one? | no | `backup_log` |
| Is the VPN up? Which IP and location? | no | `vpn` (Gluetun) |
| Who's watching or listening right now? | no | `jellyfin`, `audiobookshelf` |
| How much storage does each user use? | no | `immich`, `nextcloud` |
| What's downloading? | no | `transmission` |
| Is photo processing done? Pause it for N hours | pause: yes | `immich` |

Integrations are off until you enable them. Adding your own is a small, well-defined change: [docs/adding-an-integration.md](docs/adding-an-integration.md).

## Requirements

- Linux with Docker Engine and the Compose plugin
- A Discord account
- For the model: about 4 GB of free RAM (CPU, slow) or an NVIDIA GPU with 4 GB+ VRAM (replies in about a second)

## Quick start

```bash
git clone https://github.com/costad3atwit/Server-Steward.git && cd server-steward
cp .env.example .env && chmod 600 .env          # Discord token, your user ID, executor token
cp config/steward.example.yaml config/steward.yaml
cp -r knowledge.example knowledge
mkdir -p data/steward && sudo chown 10001:10001 data/steward
docker compose up -d --build
docker compose exec ollama ollama pull qwen3:4b-instruct-2507-q4_K_M
docker compose exec ollama ollama pull nomic-embed-text
```

Then DM your bot "how's the server doing?". The full walkthrough, including creating the Discord bot and using a GPU, is in [docs/setup.md](docs/setup.md).

## Documentation

| Guide | What's in it |
| --- | --- |
| [Setup](docs/setup.md) | Step by step: Discord bot, tokens, model options, first run, updating |
| [Configuration](docs/configuration.md) | Every setting in `.env` and `steward.yaml` |
| [Integrations](docs/integrations.md) | Each built-in integration: what it needs and how to test it |
| [Help page](docs/help-page.md) | Writing the FAQ, the API, the example page, reverse proxy setup |
| [Security model](docs/security.md) | The safety layers, what's never offered, and the trade-offs |
| [Architecture](docs/architecture.md) | Containers, request flow, retrieval, code layout |
| [Adding an integration](docs/adding-an-integration.md) | Extend Steward for your own apps |
| [Troubleshooting](docs/troubleshooting.md) | Common problems and fixes |

## License

[MIT](LICENSE)
