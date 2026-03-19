.PHONY: build install dev test clean build-all sync

build:
	uv run python build_go.py

install: build
	uv sync

dev:
	uv sync --group dev

test:
	uv run pytest tests/ -v

clean:
	rm -rf wactx/bin/whatsapp-sync* build/ dist/ *.egg-info .pytest_cache
	find . -name __pycache__ -exec rm -rf {} +

build-all:
	uv run python build_go.py --all

sync:
	uv run wactx sync
