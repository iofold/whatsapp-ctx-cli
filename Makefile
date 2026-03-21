.PHONY: build install dev test clean build-all sync dist dist-clean check

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

dist: build-all
	uv run python -m build --wheel

dist-clean:
	rm -rf build/ dist/ *.egg-info whatsapp_ctx_cli.egg-info

check:
	uv run twine check dist/*

sync:
	uv run wactx sync
