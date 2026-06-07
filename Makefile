.PHONY: check ci lint format typecheck test

# `make check` is the local gate. It runs every gate CI runs (see
# .github/workflows/ci.yml) so a green check here guarantees green CI. Keep this
# in lockstep with ci.yml.
check: lint typecheck test
	@echo "All local gates passed (CI parity: lint + format + mypy + syntax + tests)."

# Mirror of the CI lint job exactly.
lint:
	ruff check claude-mv
	ruff format --check claude-mv

# Auto-fix formatting (what `lint` checks).
format:
	ruff format claude-mv

# Mirror of the CI typecheck job.
typecheck:
	mypy claude-mv

# Mirror of the CI test job: syntax check, version smoke, then the suite.
test:
	python -c "import py_compile; py_compile.compile('claude-mv', doraise=True)"
	python claude-mv --version
	python -m pytest tests/ -q

# Alias matching ci.yml step-for-step (lint + typecheck + test jobs).
ci: lint typecheck test
