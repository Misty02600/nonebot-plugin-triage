set windows-shell := ["powershell", "-NoProfile", "-Command"]

default:
    @just --list

run:
    uv run nb run --reload

maintainer *args:
    uv run --group maintainer python -m tools.nbtriage_maintainer {{ args }}

mlflow-server host="127.0.0.1" port="5000":
    uv run --group maintainer mlflow server --host {{ host }} --port {{ port }} --workers 1 --backend-store-uri sqlite:///mlflow.db --artifacts-destination ./mlartifacts

test:
    uv run pytest

lint:
    uv run ruff check .

format:
    uv run ruff check . --fix
    uv run ruff format .

typecheck:
    uv run basedpyright

check:
    uv sync --all-groups --all-extras
    uv run ruff check .
    uv run ruff format --check .
    uv run basedpyright
    uv run pytest

hooks:
    uv run prek install

update:
    uv lock --upgrade
    uv run prek auto-update

changelog:
    uv run git-cliff --unreleased

build:
    uv build --wheel

bump *args:
    uv run cz bump --version-files-only --yes {{ args }}
    uv lock
