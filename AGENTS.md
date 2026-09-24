---
purpose: Project rules for AI agents
updated: 2026-09-17
source: generated-by-zcode-starterkit
---

# AGENTS.md

## Purpose

Maintain the Python-first ChatGPT registration and Codex OAuth tool without leaking secrets, breaking driver/provider compatibility, or triggering live external side effects during routine verification.

## Reading Order

1. This `AGENTS.md` and the task-specific nearby code/tests.
2. `README.md`, `.env.example`, and relevant files under `config/`; for deployment changes also read the README "Production Docker / CI-CD" section, `compose.yaml`, `Dockerfile`, `deploy/deploy.sh`, and `.github/workflows/ci-cd.yml`.
3. `.codex/memory/project/tech-stack.md` and `.codex/memory/project/project.md`; consult preserved legacy `.zcode/` memory only when needed.
4. Code and tests; external guidance applies only when it matches this repository.

## Stack and Structure

- Python 3.10+ with Flask, `curl_cffi`, Playwright, Selenium, CloakBrowser, and standard-library `unittest`. CI runs Python 3.12 and Node 22.
- Node.js 18+ runs the CommonJS Sentinel/PoW helper; there is no `package.json`.
- Entry points: `main.py` (CLI), `web.py` (local WebUI dev server), `wsgi.py` (production gunicorn app; runs startup reconciliation of interrupted registration jobs and stale Codex retry states), `core/codex_agent.py`, and `sentinel/sentinel-runner.js`.
- Boundaries: `config/` owns defaults/env wiring, `core/` owns domain and integration logic, `webui/` owns HTTP/UI orchestration, and `tests/` mirrors behavior areas.
- Deployment surface: `Dockerfile`, `compose.yaml`, `docker-entrypoint.sh`, `deploy/deploy.sh`, `deploy/nginx/`, and `.github/workflows/ci-cd.yml`. `artifacts/pay153_port/baseline/` is a tracked snapshot of an older tree — never edit it and exclude it from lint reasoning.

## Core Coding Contract

- Read repo instructions, docs, configs, and nearby code before editing; prefer existing patterns and the smallest correct diff.
- Preserve current public APIs, configuration shapes, persistence formats, and external side effects only when they are part of the current requirements; do not add backward-compatibility layers, migrations, or fallbacks for obsolete behavior.
- Do not add dependencies, frameworks, broad refactors, or generated churn unless the task requires them.
- Run the relevant repository commands after meaningful changes, review tracked and untracked diffs, and remove debug leftovers.
- Report skipped or failed verification exactly; never claim unverified success.

## Engineering Principles

These principles apply to implementation and architecture decisions:

1. Do not preserve backward compatibility. Delete obsolete code directly; do not add compatibility layers, write migrations, or leave fallbacks.
2. Choose the simplest implementation that satisfies the current requirements. Avoid speculative abstractions and unnecessary configuration layers.
3. Keep the system layered for the long term. First make a minimal end-to-end version work, then add complexity. Never dismantle working code for unfinished complexity.
4. Keep components modular and separate concerns.
5. Prefer mature, actively maintained libraries. Do not rewrite established capabilities without a clear reason.
6. Inspect what existing dependencies can already do before adding packages or writing custom code. Do not assume a library is unavailable.
7. Make architecture decisions for the long term. Do not accept temporary solutions framed as “we can replace this later.”
8. First study how mature products solve the same problem and use proven patterns; do not invent from scratch.

## Coding Standards (apply strictly)

- **Source:** LLM Wiki cross-language cookbook (`C:\Users\mrc\Documents\projects\agent-wiki`).
- One file is one responsibility. A module name must describe one concern; cohesion wins over mechanical grouping by symbol type.
- Any one of these six signals requires a split proposal: **multi-role identity**, **section-header navigation past unrelated sections**, **unrelated pile-up**, **cross-domain import surface**, **repeated unrelated edits in different places**, or a **god symbol** handling multiple input domains or output shapes.
- Split at the responsibility boundary, not at a line count. Refuse catch-all `utils`, `helpers`, `common`, `misc`, or `shared` bags, grab-bag public surfaces, giant regression files, and “one more function” additions after a split signal fires.
- Repository module conventions override generic heuristics; language-specific rules may strengthen but never weaken these structural rules.
- Before finalizing, scan every touched file. If any split signal fires: surface a same-turn proposal naming the seam; pause for the user to decide; if declined, record the rationale in a short file-head `ai-note`; if approved, split in the same pass when practical, run verification, and report the new boundaries. Do not silently refactor or silently leave the violation.
- Otherwise, use repository conventions, keep the smallest behavior-complete diff, preserve APIs, add or update tests for behavior changes, keep comments factual and sparse, and validate errors and edge cases.

## Selected Guideline Packs

- **Strong:** local cross-language cookbook and Python deep cookbook; JavaScript deep cookbook for `sentinel/` and WebUI script changes.
- **Adjacent:** API/security guidance for authenticated Flask routes, secret handling, and untrusted input boundaries.
- **Ignored:** TypeScript/frontend framework packs and minified SDK style; this repository has no TypeScript or Node package surface.

## Repo-Specific Rules

- Match surrounding Python: UTF-8 files, `snake_case`, `PascalCase`, `UPPER_SNAKE_CASE`, modern type hints, and concise Chinese user-facing messages/comments where the module already uses them.
- Group imports as future, standard library, third-party, then local. Keep optional browser and OS-specific imports inside the branch that uses them.
- Put public defaults in uppercase `config/*.py` constants and wire secrets through `.env` helpers. Never commit real keys, tokens, mailboxes, account exports, or Codex credentials.
- Use module loggers and domain errors; catch specific exceptions and chain translated failures with `raise ... from exc`.
- Add tests in `tests/test_<area>.py` using `<Area>Tests(unittest.TestCase)`, `test_<behavior>`, and patches at the dependency use site.
- Preserve CommonJS, strict mode, semicolons, two-space indentation, and `camelCase` in handwritten Sentinel code. Do not reformat `sentinel/sdk.js`.

## Boundaries and Gotchas

- Registration/OAuth code touches external services and account state. The agent **is permitted** to run `python main.py` and submit WebUI jobs when needed to confirm end-to-end functionality (owner-authorized). Each run consumes real resources (SMS credit, email slot, proxy quota) — run deliberately, not repeatedly as a generic smoke test.
- WebUI auth and secret endpoints are security-sensitive; preserve header/cookie checks and add negative tests for bypass attempts.
- Runtime data belongs outside tracked source. Check `.gitignore` before introducing new generated paths.
- Dynamic configuration reads such as `from config import email as _email_cfg` preserve WebUI hot reload; binding mutable values directly can leave stale settings.
- Persistence changes must preserve coordinated JSON and TXT outputs, and `accounts_viewer.html` must be treated as a credential-bearing export. `turb.sqlite3` is the source of truth; JSON/TXT/HTML exports are artifacts regenerated from it.
- Production runs as non-root uid 1000 in a read-only container: all runtime state lives in the `turb_gpt_runtime` volume at `/var/lib/turb` (symlinked over repo paths such as `turb.sqlite3`, `accounts/`, and the Chinese-named mailbox/token files listed in `Dockerfile`). New generated paths must be added to that symlink list or they will fail at runtime in the container.
- WebUI config saves go to the `runtime_config` document in `turb.sqlite3`; the production secret file (`/run/secrets/turb.env`, mode 600 on the server) is read-only bootstrap input and is never rewritten. Nginx terminates HTTPS and proxies to `127.0.0.1:5057` — never publish port 5057 directly.
- CI deployment gate tests (`tests.test_app_state_db`, `tests.test_read_only_runtime_exports`, `tests.test_sqlite_state_migration`, `tests.test_wireproxy_runtime`, `tests.test_webui_auth`) must pass locally before pushing; the deploy pipeline refuses a server checkout with uncommitted changes and only pulls immutable `sha-<commit>`/`main` GHCR images.
- Ruff is not clean; the 2026-09-17 scan reported 1,595 findings including 1,545 inside the `artifacts/` snapshot (all 19 F821 undefined-name findings live there). The live tree reports ~50 findings with `--exclude artifacts`. Do not mix unrelated cleanup into feature work.

## Verified Commands

- `python -m pip install -r requirements.txt`
- `python -m pip check`
- `python -m unittest discover -s tests -p 'test_*.py' -v`
- CI gate subset: `python -m unittest -v tests.test_app_state_db tests.test_read_only_runtime_exports tests.test_sqlite_state_migration tests.test_wireproxy_runtime tests.test_webui_auth`
- `python -m compileall -q main.py web.py wsgi.py core config webui tests`
- `python -X utf8 main.py --help` and `python -X utf8 web.py --help`
- `node --check sentinel/sentinel-runner.js && node --check sentinel/sdk.js`
- `ruff check . --exclude .zcode,.beads,.codex,artifacts` (known non-clean baseline; report the live result)
- Local WebUI on Windows: `start-local.bat -Port 5057` (bootstraps `.venv`, copies `.env.example`, force-closes the port first; `-CheckOnly` verifies without starting).

## Code Example

```python
def _is_success(result: dict) -> bool:
    """判断单次注册结果是否成功，集中收敛批量统计规则。"""
    return isinstance(result, dict) and bool(result.get("success"))
```

## Source Notes

- Wiki pages read: `queries/coding-standards-cross-language-cookbook.md`, `queries/coding-standards-programming-languages-cookbook.md`, `queries/coding-standards-programming-languages-python-cookbook.md`, and `queries/coding-standards-programming-languages-javascript-cookbook.md`.
- Reopen the vault through the `obsidian` skill for deeper rules; repository code and docs take precedence.
- No local `agent-skills-standard` checkout or repository-specific AI rules existed during setup.
- Open question: no lockfile or committed formatter/linter configuration establishes a reproducible lint policy yet.
