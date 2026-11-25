# Knowledge folders

Server Steward answers questions only from Markdown files you write. Copy this folder to
`knowledge/` (or wherever `STEWARD_KNOWLEDGE_DIR` points) and replace the examples.

| Folder | Read by | Audience | Put here |
| --- | --- | --- | --- |
| `admin/` | The Discord admin agent | You (the owner) | How your server is set up, what each stack does, what restarting it breaks, maintenance notes. |
| `help/` | The help page (`POST /help`) | Anyone who can reach the help page | Public FAQ only: how to get access, which address each service uses, how to use the apps, who to contact. |

Rules that matter:

- **Never put secrets in either folder** (passwords, tokens, keys). Help docs are shown to users; admin docs are sent to the model.
- **Keep `help/` free of internals** (IP addresses, hardware, file paths, security setup). The help assistant can only repeat what is in these files, so if it isn't here it can't leak.
- One topic per file, one question per `##` heading. Retrieval works on heading-sized chunks, and the heading becomes the source title shown to users, so write headings as the question people ask ("Add a new phone or laptop", not "Devices").
- Plain Markdown. `#`, `##` and `###` headings split chunks; headings inside code blocks are ignored.

Changes are picked up within a minute; no restart needed. To check what a question retrieves:

```bash
docker compose exec steward python -m steward_agent.cli search help "how do I add my phone"
docker compose exec steward python -m steward_agent.cli ask "how do I add my phone"
```
