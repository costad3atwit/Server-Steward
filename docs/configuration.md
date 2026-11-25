# Configuration

Two files hold everything specific to your server:

- **`.env`**: secrets and paths, read by Docker Compose. Keep it `chmod 600` and never commit it.
- **`config/steward.yaml`**: what Steward may touch and how it behaves. Mounted read-only into both containers.

After editing `steward.yaml`, DM the bot "reload config" (or run `docker compose restart`). The executor checks the file strictly: unknown keys, bad names and out-of-range values are refused with a message naming the field, and the previous config stays active. After editing `.env`, run `docker compose up -d` (not `restart`, which keeps the old environment).

## `.env`

| Variable | Required | Used by | Meaning |
| --- | --- | --- | --- |
| `DISCORD_BOT_TOKEN` | yes | agent | Your bot's token |
| `DISCORD_OWNER_IDS` | yes | agent | Comma-separated Discord user IDs allowed to give commands |
| `EXECUTOR_TOKEN` | yes | both | Shared secret, 32+ characters (`openssl rand -hex 32`) |
| `COMPOSE_PROFILES` | | compose | `ollama` runs the bundled Ollama; remove to use your own |
| `COMPOSE_FILE` | | compose | Extra compose files, e.g. `compose.yaml:compose.gpu.yaml:compose.local.yaml` |
| `OLLAMA_URL` | | agent | Where Ollama is (default `http://ollama:11434`) |
| `OLLAMA_KEEP_ALIVE` | | ollama | How long an idle model stays loaded (default `10m`) |
| `OLLAMA_KV_CACHE_TYPE` | | ollama | `q8_0` (default) halves context memory; `q4_0` saves more |
| `TZ` | | both | Timezone for displayed times, e.g. `America/Chicago` |
| `STEWARD_CONFIG_DIR` | | both | Folder holding `steward.yaml` (default `./config`) |
| `STEWARD_KNOWLEDGE_DIR` | | agent | Your knowledge folders (default `./knowledge`) |
| `STEWARD_DATA_DIR` | | both | Audit logs, schedules, search index (default `./data`) |
| `OLLAMA_DATA_DIR` | | ollama | Downloaded models (default `./data/ollama`) |
| `HELP_BIND` | | agent | Where the help API listens (default `127.0.0.1:8088`) |
| `DOCKER_SOCKET` | | executor | Docker socket path (default `/var/run/docker.sock`) |
| `STEWARD_IMAGE_OWNER`, `STEWARD_VERSION` | | compose | Where to pull prebuilt images from |
| `IMMICH_API_KEY` | | executor | For the `immich` integration |
| `JELLYFIN_API_KEY` | | executor | For the `jellyfin` integration |
| `AUDIOBOOKSHELF_TOKEN` | | executor | For the `audiobookshelf` integration |
| `TRANSMISSION_USER`, `TRANSMISSION_PASS` | | executor | For `transmission`, if its web UI has a login |

App credentials go only to the executor; the agent, which talks to Discord and the model, never receives them.

## `steward.yaml`

### `llm` (agent)

| Key | Default | Meaning |
| --- | --- | --- |
| `chat_model` | `qwen3:4b-instruct-2507-q4_K_M` | Ollama model for conversation and tool calls. Must support tool calling. |
| `embed_model` | `nomic-embed-text` | Ollama model for searching your knowledge folders |
| `temperature` | `0.2` | Lower is more predictable |
| `options` | `{}` | Passed to Ollama with every chat request. `num_ctx: 8192` sets the context; `num_gpu: 99` puts every layer on the GPU (remove it if the model fails to load) |

### `confirm_timeout_seconds` (agent)

How long a ✅ prompt stays valid. Default 300, allowed 30 to 3600.

### `rag` (agent): knowledge search

| Key | Default | Meaning |
| --- | --- | --- |
| `top_k` | 4 | Sections passed to the model per question |
| `min_score` | 0.6 | Minimum similarity (0 to 1) for a section to count. Below it, the help page doesn't ask the model at all. Tune it with the `search` command in [help-page.md](help-page.md#tuning) |
| `doc_prefix`, `query_prefix` | `search_document: `, `search_query: ` | Task prefixes `nomic-embed-text` expects; set both to `""` for other embedding models |
| `embed_options` | `{num_gpu: 0}` | Runs embeddings on the CPU so they never push the chat model off a small GPU |

### `help` (agent): help page

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | true | Serve the help API |
| `owner_name` | `the server admin` | How answers refer to you ("message Sam") |
| `max_question_chars` | 500 | Longest question accepted |
| `rate_limit_per_minute` | 10 | Questions per client IP per minute |
| `forward_unanswered` | true | DM you questions it can't answer. Turn off if your page has its own contact form |
| `max_forwards_per_hour` | 20 | Cap on forwarded questions |
| `max_answer_tokens` | 400 | Longest answer |
| `contact_form` | true | Serve `POST /message`, a plain relay to your DMs |
| `contact_messages_per_hour` | 5 | Contact messages per client IP per hour |

### `status` (executor)

```yaml
status:
  disks:
    - name: root
      path: /            # paths inside the executor; mount host paths under /host
    - name: media
      path: /host/media
  extra_containers: [caddy]   # shown in status and allowed for logs, but not restartable
```

### `stacks` (executor)

Each stack is a name the model can use, a description, and the containers to restart in order.

| Step key | Default | Meaning |
| --- | --- | --- |
| `container` | required | Exact container name |
| `wait_healthy` | false | Wait until Docker reports it healthy (or running, if it has no healthcheck) before the next step |
| `timeout_seconds` | 120 | How long to wait (5 to 900) |
| `retries` | 0 | Restart again if it isn't healthy in time (0 to 3). Useful for VPN clients stuck on a bad server |

If a step never becomes healthy, the remaining steps are skipped and you're told which step failed.

Stack names: lowercase letters, digits, `-` and `_`.

### `integrations` (executor and agent)

Each integration has `enabled` plus its own settings; see [integrations.md](integrations.md). The agent only offers the model a tool or topic when its integration is enabled.
