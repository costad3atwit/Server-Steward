# Contributing

Thanks for helping. Issues, documentation fixes and new integrations are all welcome.

## Development

Python 3.12. Each container is its own package with its own tests, and neither needs Docker, Discord or a model to test:

```bash
cd executor && pip install -r requirements.txt pytest && python -m pytest -q
cd ../steward && pip install -r requirements.txt pytest && python -m pytest -q
```

To run the whole thing, follow [docs/setup.md](docs/setup.md) and use `docker compose up -d --build`.

## Guidelines

- **New integrations** follow [docs/adding-an-integration.md](docs/adding-an-integration.md): reader and verb in the executor, tool or topic in the agent, a formatter, tests on both sides, and a section in `docs/integrations.md`.
- **The executor is the security boundary.** Anything that changes state validates every argument against the config, takes the mutate lock, and is offered to the model with `risk="confirm"`. New verbs that run arbitrary commands, delete data, create containers or change host configuration won't be accepted.
- **Secrets go to the executor only**, never to the `steward` service.
- **Results are formatted in code**, never by the model.
- **Prompts stay general**: no facts about any particular server.
- Add a test for every bug you fix, especially ones found by talking to a real model.
- Keep tool counts low: prefer a new topic or action on an existing tool.

## Releases

Pushing a tag like `v0.2.0` builds and publishes both images to the GitHub Container Registry (`.github/workflows/release.yml`). Update `CHANGELOG.md` and the `__version__` strings first.
