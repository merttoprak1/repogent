.PHONY: test lint typecheck security verify validator-image

test:
	python -m pytest

lint:
	ruff check .

typecheck:
	mypy

security:
	bandit -q -r src/repogent

verify: test lint typecheck security

validator-image:
	docker build -t repogent-validator:py311 -f docker/validator.Dockerfile .
