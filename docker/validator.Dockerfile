FROM python:3.11.13-slim

RUN useradd --create-home --uid 10001 validator \
    && python -m pip install --no-cache-dir \
       bandit==1.8.6 fastapi==0.116.1 httpx==0.28.1 mypy==1.17.1 \
       pytest==8.4.1 ruff==0.12.5

USER validator
WORKDIR /workspace
ENTRYPOINT []
