.PHONY: test lint format-check typecheck security package-check verify validator-image

PYTHON ?= python3
PACKAGE_TESTS := \
	tests/unit/test_package_data.py \
	tests/unit/test_plugin_package.py \
	tests/integration/test_plugin_end_to_end.py

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .

format-check:
	$(PYTHON) -m ruff format --check .

typecheck:
	$(PYTHON) -m mypy src

security:
	$(PYTHON) -m bandit -q -r src/repogent

package-check:
	$(PYTHON) -m build
	REPOGENT_PACKAGE_CHECK=1 $(PYTHON) -m pytest $(PACKAGE_TESTS) -q --no-cov

verify: test lint format-check typecheck security package-check

validator-image:
	docker build -t repogent-validator:py311 -f docker/validator.Dockerfile .
