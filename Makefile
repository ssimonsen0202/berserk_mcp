.PHONY: lint lint-fix lint-ci format format-check test coverage coverage-ci typecheck typecheck-strict

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

typecheck:
	mypy --config-file pyproject.toml berserk_mcp.py ai_finops.py kql_validation.py parser_factory.py _store.py _http.py

typecheck-strict:
	mypy --config-file pyproject.toml --disallow-untyped-defs berserk_mcp.py ai_finops.py kql_validation.py parser_factory.py _store.py _http.py
