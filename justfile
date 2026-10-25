# PyVised development commands. Run `just` to list them.
# Recipes run in Windows PowerShell, so Docker arguments are not rewritten the way Git Bash does.

set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]

image     := "pyvised-backend"
dev_image := image + ":dev"

# list the recipes
default:
    @just --list

# create / refresh the development environment and build the translations
setup:
    uv sync --all-packages
    just i18n-build

# start the desktop app against the locally built backend image
run *args:
    $env:PYVISED_BACKEND_IMAGE = "{{dev_image}}"; uv run python -m pyvised {{args}}

# lint and check formatting
lint:
    uv run ruff check .
    uv run ruff format --check .

# format the code and apply safe lint fixes
fmt:
    uv run ruff format .
    uv run ruff check --fix .

# type-check the client, protocol and backend sources
typecheck:
    uv run pyright

# client, protocol and backend tests on Windows (OpenMC tests are skipped here)
test: i18n-build
    $env:QT_QPA_PLATFORM = "offscreen"; uv run pytest

# build the backend image (OpenMC 0.16.0 from conda-forge + the PyVised service)
backend-build:
    docker build -f backend/Dockerfile --target runtime -t {{dev_image}} .

# run the backend and protocol tests inside the container, with real OpenMC
backend-test:
    docker build -f backend/Dockerfile --target test -t {{image}}:test .
    docker run --rm {{image}}:test

# run the backend alone on http://127.0.0.1:8765 (token "dev"), reloading on code changes
backend-dev:
    docker run --rm -it -p 127.0.0.1:8765:8765 -e PYVISED_TOKEN=dev -e WATCHFILES_FORCE_POLLING=true -e PYTHONPATH=/src/backend/src:/src/protocol/src -v "${PWD}\backend\src:/src/backend/src" -v "${PWD}\protocol\src:/src/protocol/src" -v "$env:USERPROFILE\PyVised:/workspace" {{dev_image}} --reload

# open a shell inside the backend image
backend-shell:
    docker run --rm -it --entrypoint /usr/local/bin/_entrypoint.sh {{dev_image}} bash

# collect the translatable strings into the Hungarian .ts file
i18n-update:
    uv run pyside6-lupdate -extensions py -recursive app/src/pyvised -ts app/src/pyvised/i18n/pyvised_hu.ts

# compile the .ts translations into .qm files
i18n-build:
    uv run pyside6-lrelease app/src/pyvised/i18n/pyvised_hu.ts

# edit the Hungarian translation in Qt Linguist
i18n-edit:
    uv run pyside6-linguist app/src/pyvised/i18n/pyvised_hu.ts
