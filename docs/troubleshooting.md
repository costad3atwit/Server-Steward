# Troubleshooting

Start with the logs:

```bash
docker compose ps
docker compose logs --tail 50 steward steward-executor
tail -n 20 data/steward/audit.log data/executor/audit.log
```

## Discord

**The bot is online but never answers.** `DISCORD_OWNER_IDS` must be your user ID, not the bot's ID or a server ID. Messages from anyone else are ignored on purpose and logged as `"event": "ignored"` in the agent's audit log.

**The bot never comes online.** Check the token: `docker compose logs steward | grep -i -E 'login|token|401'`. If you reset the token in the Developer Portal, the old one stopped working everywhere it was used.

**Warnings about intents, PyNaCl or voice** are harmless: Steward only uses DMs.

## Model

**"The model isn't responding".** Check Ollama is running and the model is downloaded:

```bash
docker compose logs --tail 20 ollama
docker compose exec ollama ollama list
```

The names must match `llm.chat_model` and `llm.embed_model` exactly.

**Replies are very slow.** Without a GPU, several seconds is normal. With one, check the whole model is on it: `docker compose exec ollama ollama ps` should say `100% GPU`. If part is on the CPU, keep `num_gpu: 99` under `llm.options`; if the model then fails to load, it doesn't fit, so use a smaller model or a smaller `num_ctx`.

**The first message is slow.** The model unloads after `OLLAMA_KEEP_ALIVE` (10 minutes) idle and takes about 30 seconds to load again. Raise the keep-alive if you prefer speed over idle power.

**It thinks out loud, or takes 10+ seconds to pick an action.** You're using a thinking model. Switch `chat_model` to an instruct (non-thinking) variant.

**It picked the wrong action.** Look at the `plan` line in `data/steward/audit.log`: it shows the text, the tool and arguments the model chose, and how long it took. Clearer stack `description`s and notes in `knowledge/admin/` help most.

**A GPU never powers down when idle.** Something is holding the device open: `sudo fuser -v /dev/nvidia*`. Monitoring tools that poll the GPU (btop, nvtop) are common culprits.

## Executor

**Exits with "EXECUTOR_TOKEN must be set".** Generate one with `openssl rand -hex 32` and put it in `.env`.

**Config errors on start, or "config not reloaded".** The executor refuses unknown keys and bad values on purpose; the log names the field. Common causes: wrong indentation, a misspelt key such as `wait_healty`, a stack name with capitals.

**Config changes don't take effect.** DM "reload config", or `docker compose restart`. If you edited `.env`, use `docker compose up -d`.

**"Unauthorized" when testing with curl.** The `TOKEN` variable is empty in a new shell: set it again from `.env`.

**A restart reports "not healthy after N attempt(s)".** The container's healthcheck didn't pass in time. Check `docker inspect <name> --format '{{json .State.Health}}'`, raise `timeout_seconds`, or add `retries`. The later steps were skipped deliberately, so start them yourself if needed.

## Integrations

**"Temporary failure in name resolution".** The executor isn't on the app's network. Check `docker inspect server-steward-steward-executor-1 --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}'`, then the `networks:` list under `steward-executor` in `compose.local.yaml` (both the service list and the top-level definition), and that `compose.local.yaml` is in `COMPOSE_FILE`.

**"... is not set" for a key or token.** The value is empty inside the executor. Check `.env`, then `docker compose up -d steward-executor` so it's re-read.

**HTTP 401 or 403 from an app.** The key is wrong or lacks permission; Immich and Audiobookshelf need an admin user's key.

**Immich pause returns HTTP 404.** Your Immich version uses a different jobs API. Open an issue with your version.

**Backup status is wrong or empty.** Check the mounted path exists inside the executor (`docker compose exec steward-executor ls -l /host/backup/`) and that your log lines start with `YYYY-MM-DD HH:MM:SS` and match the patterns in `backup_log`.

## Help page

**It says it couldn't find an answer to everything.** The knowledge folder may not be mounted: `docker compose exec steward python -m steward_agent.cli chunks help` should list your sections. If it shows 0, check `STEWARD_KNOWLEDGE_DIR` and run `docker compose up -d steward`.

**It answers questions it shouldn't, or refuses ones it should answer.** See [tuning](help-page.md#tuning).

**The page shows "isn't reachable".** Check your reverse proxy route to `127.0.0.1:8088` and `docker compose logs steward | grep 'help endpoint'`.

**Styling changes don't appear.** Browsers cache CSS; add a version to the link (`style.css?v=2`) and bump it on each change.
