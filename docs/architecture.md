# Architecture

Three containers in one Docker Compose project.

```
                        ┌──────────────────────────────┐        ┌───────────────────────┐
 Owner (Discord DM) ───▶│ steward (agent)              │───────▶│ ollama                │
                        │  Discord bot + help API      │prompts │  chat model (GPU/CPU) │
 Help page ─▶ proxy ───▶│  knowledge search (RAG)      │◀────── │  embedding model      │
             /api/*     │  tool schemas from config    │        └───────────────────────┘
                        │  validation + ✅ confirmation │◀── knowledge/ (read only)
                        └──────────────┬───────────────┘
                                       │ POST /v1/action {verb, args}, bearer token
                                       │ internal-only network
                        ┌──────────────▼───────────────┐
                        │ steward-executor             │
                        │  fixed verb list             │
                        │  re-checks steward.yaml      │
                        │  Docker socket + app keys    │
                        └──────────────┬───────────────┘
                     Docker API, Immich, Jellyfin, Audiobookshelf, Transmission, ...
```

| Container | Image | Job |
| --- | --- | --- |
| `steward` | `steward/` | Discord bot and help API on one asyncio event loop. Talks to the model, searches your knowledge, validates tool calls, asks for ✅, formats results. Holds no Docker or app credentials. |
| `steward-executor` | `executor/` | A small FastAPI service with a fixed verb list. The only component with the Docker socket and app API keys. Keeps scheduled actions (such as automatic resumes) in its data folder. |
| `ollama` | `ollama/ollama` | Optional (`COMPOSE_PROFILES=ollama`). Serves the chat and embedding models. Replace with your own Ollama via `OLLAMA_URL`. |

Networks: `steward-internal` (internal, no internet) joins the agent and executor; `llm` joins the agent and Ollama; the agent also has a default network for Discord. The executor joins app networks only through `compose.local.yaml`.

## An admin request, step by step

1. A DM arrives. If the author isn't in `DISCORD_OWNER_IDS`, it's logged and ignored.
2. If the model isn't loaded, the bot says it's waking up (about 30 seconds).
3. The agent searches `knowledge/admin/` and adds matching sections as reference notes, marked as information rather than instructions.
4. The model gets the admin prompt, the notes, the message and the tool definitions generated from `steward.yaml`. It returns one tool call or a plain answer.
5. The agent validates the call against its JSON schema and applies grounding checks (for example, durations from the user's words). Grouped tools are routed to an executor verb in code.
6. Read-only verbs run immediately. Others post a ✅/❌ prompt and wait.
7. The executor validates the request again, runs it, and returns a result.
8. The agent formats the result in code and replies. Both sides write audit lines.

## A help question, step by step

1. `POST /help` with the question, rate limited per client.
2. The question is embedded and compared with `knowledge/help/` sections. Nothing above `rag.min_score` means no answer, without calling the model.
3. The top sections go to the model with the help prompt and **no tools**. The model answers and lists which excerpts it used, or replies `UNSURE`.
4. Code checks every web address against the excerpts (near-misses corrected, anything else withheld) and keeps only cited sources.

## Knowledge search (RAG)

- Markdown is split at `#`, `##` and `###` headings (ignoring code blocks); the heading path becomes the source title. Long sections are split at blank lines.
- Sections are embedded with `nomic-embed-text` on the CPU, so the chat model keeps the GPU.
- Vectors are stored as JSON in `data/steward/index/`, one file per collection, and rebuilt when files change (checked every minute). The corpus is small, so search is a plain cosine similarity loop: no vector database.

## Tool design for small models

Every tool definition is sent with every message, and small models choose less reliably as the list grows, so related actions share one tool with a `topic` or `action` argument (for example `get_status` with topics overview, backups, vpn, streaming). The agent maps each to an executor verb in `tools.py`. Tools for disabled integrations aren't sent at all.

## Code layout

```
steward/steward_agent/
  main.py         startup: config, clients, bot + help API + index refresh loop
  bot.py          Discord: owner check, ✅/❌ confirmation, DM forwarding
  agent.py        plan (model call, validation, grounding) and execute; help answers
  tools.py        tool registry generated from steward.yaml, schema validation, routing
  prompts.py      the general admin and help prompts
  rag.py          Markdown chunking, embedding index, search
  grounding.py    URL and source checks on help answers
  help_api.py     /help, /help/status, /message
  formatting.py   executor results -> Discord text
  cli.py          search / ask / chunks for tuning
executor/steward_executor/
  main.py         HTTP API, auth, audit log, scheduler loop
  actions.py      VERBS and one _v_<verb> method each: the whole of what Steward can do
  readers.py      read-only integrations (backups, VPN, storage, torrents, streams, logs)
  immich.py       Immich job commands
  config.py       strict schema for steward.yaml
  schedules.py    persisted one-shot schedules
  hostinfo.py     uptime, load, memory, disks, temperatures
```
