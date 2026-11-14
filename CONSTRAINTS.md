# Constraints

This standalone extraction retains the NexaAssist quality requirements relevant
to banking. It does not change the original repository's CONSTRAINTS.md or gates.

- No checker suppressions, skipped/deleted tests, stripped assertions, unfinished
  stubs or empty exception handlers. Do not weaken this contract to pass a change.
- No credentials in Git, images, logs, fixtures or scan output.
- `make check-fast`: zero Ruff, formatting and mypy errors in application and tests.
- `make check-task`: 100% application line coverage, with no coverage exclusions.
  All application lines are new to this repository; this also satisfies the
  original 100% changed-line requirement and exceeds its 84.13% overall floor.
- `make check-full`: zero ERROR findings and zero scan errors from Semgrep CE
  1.182.1 using p/default, p/owasp-top-ten and p/secrets; no baseline exceptions.
- `make test-atlas`: explicit transaction/persistence check against a newly
  generated, test-owned database. Fail if no URI is configured; never silently skip.
- Routine checks target 90 seconds; dependency downloads, container builds and
  live Atlas/provider verification run separately.

Hosted OAuth, Python 3.14 support, custom-header forwarding and owner/reviewer
integration require actual provider verification before endpoint migration.
Unit doubles do not establish MongoDB transaction guarantees.

RAG/model release evaluations and UI checks belong to NexaAssist, which remains
unchanged; this repository has no LLM, RAG, Streamlit or approval UI.
