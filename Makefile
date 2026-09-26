.PHONY: lint lint-fix lint-ci format format-check test coverage coverage-ci typecheck typecheck-strict inspector-check

lint:
	ruff check .

lint-fix:
	ruff check --fix .

lint-ci:
	ruff check --output-format=github .

format:
	ruff format .

format-check:
	ruff format --check .

test:
	python3 -m unittest discover -s tests

coverage:
	python3 -m coverage run -m unittest discover -s tests
	python3 -m coverage report
	python3 -m coverage html

coverage-ci:
	python3 -m coverage run -m unittest discover -s tests
	python3 -m coverage report --fail-under=80

# Every shipped module: must equal pyproject.toml py-modules (enforced by
# tests/test_packaging.py). mypy 2.x cannot target Python 3.9, so it checks
# as 3.10 (pyproject [tool.mypy]); the 3.9 floor is guarded by ruff's py39
# target (it rejects 3.10+ syntax) and the Python 3.9 CI test jobs.
TYPED_MODULES = berserk_mcp.py parser_factory.py agent_analytics.py ai_finops.py secret_scan.py ingestion_advisor.py kql_validation.py schema_registry.py _store.py _http.py tool_discovery.py quota_status.py investigation.py tool_catalog.py model_drift.py _kql_boundary.py _tag_guard.py

typecheck:
	mypy --config-file pyproject.toml $(TYPED_MODULES)

typecheck-strict:
	mypy --config-file pyproject.toml --disallow-untyped-defs $(TYPED_MODULES)

# Optional, needs Node >= 22.19; not part of required CI. See scripts/inspector_check.py.
inspector-check:
	python3 scripts/inspector_check.py
