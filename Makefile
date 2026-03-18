.PHONY: build install dev test clean build-all

build:
	python build_go.py

install: build
	pip install -e .

dev:
	pip install -e ".[dev]"

test:
	python -m pytest tests/ -v

clean:
	rm -rf wactx/bin/ build/ dist/ *.egg-info .pytest_cache
	find . -name __pycache__ -exec rm -rf {} +

build-all:
	python build_go.py --all
