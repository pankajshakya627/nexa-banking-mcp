UV = uv run --locked --no-sync

.PHONY: check-fast check-task check-full test-atlas
check-fast:
	$(UV) ruff check app tests
	$(UV) ruff format --check app tests
	$(UV) mypy app tests

check-task: check-fast
	$(UV) pytest --cov=app --cov-report=term-missing --cov-report=xml:reports/coverage.xml --cov-fail-under=100

check-full: check-task
	mkdir -p reports
	SEMGREP_SETTINGS_FILE=reports/semgrep-settings.yml uv tool run --python 3.13 --from semgrep==1.182.1 semgrep scan --config p/default --config p/owasp-top-ten --config p/secrets --metrics off --disable-version-check --disable-nosem --severity ERROR --strict --error --json --output reports/security.json app Dockerfile

test-atlas:
	$(UV) pytest tests/live_atlas.py -q --tb=short
