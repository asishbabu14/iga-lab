PYTHON ?= python3
export PYTHONPATH := src:$(PYTHONPATH)
export PYTHONUNBUFFERED := 1

.PHONY: install lint format test demo ci clean

install:
	$(PYTHON) -m pip install -e ".[dev]"

lint:
	$(PYTHON) -m ruff check src tests
	$(PYTHON) -m ruff format --check src tests

format:
	$(PYTHON) -m ruff format src tests
	$(PYTHON) -m ruff check --fix src tests

test:
	$(PYTHON) -m pytest

demo:
	$(PYTHON) -m iga_lab.demo

ci: lint test demo

clean:
	rm -rf reports var .pytest_cache .ruff_cache build dist src/*.egg-info
	find src tests -type d -name __pycache__ -exec rm -rf {} +
