# Setup

This takes about 20 minutes, plus the model download. Every command runs on the server that hosts your Docker containers.

## 1. Get the files

```bash
git clone https://github.com/costad3atwit/Server-Steward.git
cd server-steward
cp .env.example .env && chmod 600 .env
cp config/steward.example.yaml config/steward.yaml
cp -r knowledge.example knowledge
```

`.env`, `config/steward.yaml`, `knowledge/` and `data/` are git-ignored, so `git pull` never overwrites them.

## 2. Create the Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**. Name it, for example "Server Steward".
2. Go to **Bot**:
   - Click **Reset Token**, copy it, and paste it into `.env` as `DISCORD_BOT_TOKEN`. Treat it like a password.
   - **Reusing a bot you already have?** Don't reset its token: resetting invalidates the old token everywhere else it's used. Copy it from wherever it's stored instead.
   - Turn **Public Bot** off so nobody else can add it.
   - Privileged intents are **not** needed.
3. Make the bot reachable by DM: under **Installation**, enable **User Install**, or generate an invite URL with the `bot` scope and add it to a private Discord server you own.
4. Find your user ID: Discord **Settings → Advanced → Developer Mode** on, then right-click your name → **Copy User ID**. Put it in `.env` as `DISCORD_OWNER_IDS` (comma-separate several owners).
5. Send the bot one DM so the conversation exists.

## 3. Generate the executor token

The agent and the executor share a secret so nothing else on the network can call the executor:

```bash
openssl rand -hex 32
```

Paste the output into `.env` as `EXECUTOR_TOKEN`. Set `TZ` too (for example `TZ=Europe/Berlin`), so scheduled times are shown in your timezone.

## 4. Choose how to run the model

| Option | Speed | Setup |
| --- | --- | --- |
| **A. Bundled Ollama on the CPU** (default) | Several seconds per reply | Nothing; keep `COMPOSE_PROFILES=ollama` in `.env` |
| **B. Bundled Ollama on an NVIDIA GPU** | About a second per reply | Driver + NVIDIA Container Toolkit, then add `compose.gpu.yaml` |
| **C. Your own Ollama** | Depends | Remove `COMPOSE_PROFILES=ollama`, set `OLLAMA_URL` |

**Option B.** Install the NVIDIA driver for your distribution and the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html), then:

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker          # restarts every container on the host
docker run --rm --gpus all ubuntu nvidia-smi   # should list your GPU
```

Add the GPU file to `.env` so every `docker compose` command includes it:

```
COMPOSE_FILE=compose.yaml:compose.gpu.yaml
```

Laptops and machines with two GPUs: if your media server uses `/dev/dri` for hardware transcoding, check that its render device is unchanged after the NVIDIA driver loads (`ls -l /dev/dri/by-path/`).

**Option C.** For Ollama running directly on the host, use `OLLAMA_URL=http://host.docker.internal:11434` and make Ollama listen on an address containers can reach (for example `OLLAMA_HOST=0.0.0.0`, firewalled from your LAN).

**Choosing a model.** Any Ollama model that supports tool calling and fits your memory works; set it as `llm.chat_model` in `config/steward.yaml`. Prefer a *non-thinking* (instruct) model: thinking models reason for hundreds of tokens before every tool call and are about ten times slower. The default, `qwen3:4b-instruct-2507-q4_K_M`, fits entirely on a 4 GB GPU with an 8k context.

`OLLAMA_KEEP_ALIVE=10m` unloads the model after 10 idle minutes, so the GPU can power down. The first message after that takes about 30 seconds, and Steward tells you it's waking up.

## 5. Describe your server

Edit `config/steward.yaml`. The example is commented; the stacks in it are examples to replace.

```yaml
stacks:
  downloads:
    description: VPN and torrent client. Restarting interrupts downloads.
    restart_sequence:
      - container: gluetun          # restarted first
        wait_healthy: true          # waits for its Docker healthcheck
        timeout_seconds: 60
        retries: 2                  # restart again if it doesn't come up
      - container: transmission     # only after the VPN is healthy
```

- Use real container names: `docker ps --format '{{.Names}}'`.
- Write `description` for a person: it's shown to the model and in the ✅ prompt, so say what a restart interrupts.
- Every container named here can also have its logs read, and appears in status.

Then write your knowledge folders (optional but recommended):

- `knowledge/admin/`: notes for the Discord assistant about how your server is set up.
- `knowledge/help/`: the public FAQ for the help page.

See [knowledge.example/README.md](../knowledge.example/README.md). Never put secrets in either.

To enable integrations (Immich, Jellyfin and so on), follow [integrations.md](integrations.md) after the first run works.

## 6. Start it

The agent runs as UID 10001 and needs to write its audit log and search index:

```bash
mkdir -p data/steward && sudo chown 10001:10001 data/steward
docker compose up -d --build
docker compose exec ollama ollama pull qwen3:4b-instruct-2507-q4_K_M
docker compose exec ollama ollama pull nomic-embed-text
docker compose logs steward | grep -E 'tools:|logged in'
```

The last line should list the tools and show the bot logged in.

## 7. Test

In a DM to the bot:

1. `how's the server doing?`: a status report, no confirmation.
2. `restart <one of your stacks>`: a ✅ prompt. React ✅ and it reports each step.
3. `delete my old photos`: it declines without doing anything.

Audit logs show what the model proposed (agent) and what actually ran (executor):

```bash
tail -f data/steward/audit.log data/executor/audit.log
```

## Updating

```bash
git pull
docker compose up -d --build
```

Read [CHANGELOG.md](../CHANGELOG.md) first: new settings appear in `config/steward.example.yaml` and `.env.example`, and your own copies are never changed for you.

## Using prebuilt images

If the project publishes images, set `STEWARD_IMAGE_OWNER` (and optionally `STEWARD_VERSION`) in `.env`, then `docker compose pull && docker compose up -d` instead of building.
