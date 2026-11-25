# Security model

Server Steward lets a language model influence a real server, so it's built on one assumption: **the model can be wrong or manipulated at any time.** It's never the thing that decides what's allowed. It proposes; code checks; you confirm; a separate, minimal container acts.

## What the model can and can't reach

| | Agent (`steward`) | Executor (`steward-executor`) |
| --- | --- | --- |
| Talks to | Discord, the help page, Ollama, the executor | Docker, the apps you enable |
| Docker socket | **No** | Yes |
| App API keys | **No** | Yes |
| Internet | Yes (Discord, model downloads via Ollama) | **No** (internal network, plus only the app networks you join) |
| Runs as | UID 10001 | root inside the container, all capabilities dropped |
| Filesystem | read-only, except its data folder | read-only, except its data folder |

The agent can only send the executor `{verb, args}` with a bearer token. The executor accepts nothing else.

## The layers

A request has to pass every layer before anything changes.

1. **Who can ask.** Only Discord DMs from `DISCORD_OWNER_IDS` are processed; everything else is ignored and logged. The help API listens on localhost by default and is rate limited per client. Nothing needs a port open to the internet.
2. **What the model is offered.** Tool definitions are generated from your config, so the model only ever sees your real stack, queue and container names. The help path is given **no tools at all**, so prompt injection through the public question box can't trigger an action.
3. **Prompts.** General behaviour prompts tell the model to act only through tools, never guess configuration, decline destructive or security-weakening requests, and treat retrieved notes as information, not instructions. Prompts are a first line of defence, not a boundary: everything below holds even if the model ignores them.
4. **Validation in the agent.** Every tool call is checked against a JSON schema. Unknown tools, unknown arguments and out-of-range values are refused before anything is sent.
5. **Grounding checks in code.** The model's output is compared with what the user actually wrote and what the docs actually say: a pause duration comes from the user's words (a model can't turn "a week" into "16 hours"); web addresses in help answers must appear in the docs; only sources the answer cited are shown.
6. **Human confirmation.** Anything disruptive (restarts, pauses) shows exactly what will run and waits for your ✅, for 5 minutes by default.
7. **Validation in the executor.** The executor re-checks every request as if it were hostile: the verb must be in its fixed list (`VERBS` in `executor/steward_executor/actions.py`), and every argument must match your config. One mutating action runs at a time.
8. **Least privilege.** See the table above. App credentials never reach the container that talks to the model.
9. **Output handling.** Results are formatted by code, never summarized by the model, so it can't misreport what ran. Container logs are redacted (`key=value` secrets, long tokens, `user:pass@` URLs) before reaching Discord. Text from the contact form is escaped so it can't ping or format.
10. **Audit.** Both containers append JSON lines to `data/*/audit.log`: every request, refusal, correction, confirmation and result.

## Never offered

These aren't in the verb list, so no prompt, document or model mistake can produce them: running shell commands, deleting files, creating containers, changing firewall rules or ports, installing software, editing configuration, managing accounts or passwords, rebooting the host.

## Trade-offs to understand

- **The executor holds the Docker socket**, which is root-equivalent on the host. That's why it's a separate, small container whose only interface is the verb list. Read `executor/steward_executor/actions.py` before you trust it; it's short. Generic socket proxies weren't used because they filter by API section, and allowing restarts also allows creating a new container that mounts the host filesystem.
- **Joining app networks** gives the executor a route to those apps (and through them, possibly further). It still holds no Discord or model credentials.
- **The Nextcloud integration uses `docker exec`.** It only runs two fixed `occ` read commands, with a user ID taken from Nextcloud's own user list.
- **`X-Forwarded-For` is trusted** for rate limiting, which is only safe while the help API is reachable solely through your reverse proxy (the default localhost binding).
- **Your knowledge files are sent to the model.** Keep secrets out of them. Help files are effectively public to everyone who can open the help page.
- **The model runs locally**, so nothing leaves your network unless you point `OLLAMA_URL` somewhere else.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting (the repository's **Security** tab) rather than a public issue. See [SECURITY.md](../SECURITY.md).
