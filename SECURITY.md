# Security policy

Server Steward lets a language model request actions on a server, so security reports are very welcome.

**Please don't open a public issue for a vulnerability.** Use GitHub's private vulnerability reporting instead: the repository's **Security** tab, then **Report a vulnerability**. Include what you found, how to reproduce it, and which version or commit you tested.

Especially interesting:

- any way to make the executor run something outside its verb list, or touch a container or value not in the config
- any way for help page input to cause an action, or to reveal admin notes, credentials or other users' data
- any way to reach the executor without the token

How the system is meant to hold up is described in [docs/security.md](docs/security.md).
