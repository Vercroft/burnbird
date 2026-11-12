# Contributing to Burnbird

This file says how you can help and which rules we follow in this repository.

## Issues and discussions

We welcome bug reports, ideas and questions. Open an issue for a bug or a concrete request, and a discussion for a
question or an idea. Please search first, in case it is already there.

Never report a security problem in public: follow [SECURITY.md](SECURITY.md).

## Pull requests

We write all the code ourselves, so pull requests are limited to us. Please open an issue instead, and we will pick it
up.

## Rules

We follow the rules in [Engineering Practices](https://github.com/Vercroft/engineering-practices), written down in its
[STANDARD.md](https://github.com/Vercroft/engineering-practices/blob/main/STANDARD.md). This file adds only what is
specific here.

## Commands

None yet: the code comes in later pull requests, and this section will then list the exact commands to install, test
and lint, each one a command that CI runs.

The commit-time hook: `pre-commit install` once, then every commit runs gitleaks with `.gitleaks.toml`.

## Map

| Path | Holds |
|---|---|
| `README.md` | What Burnbird is |
| `LICENSE` | The licence, FSL-1.1-MIT, unchanged except its copyright line |
| `SECURITY.md` | How to report a vulnerability |
| `.gitleaks.toml`, `.pre-commit-config.yaml` | The leak patterns and the commit-time hook |
| `.github/CODEOWNERS` | Who reviews every change |

## Boundaries

- Secrets never go in git.
- `LICENSE` stays the published text, with only its copyright line filled in.
- Call the licence "Fair Source (FSL-1.1-MIT)".
