.PHONY: lint lint-fix lint-ci format test

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
