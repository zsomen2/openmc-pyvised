# PyVised development commands. Run `just` to list them.
# Recipes run in Windows PowerShell, so Docker arguments are not rewritten the way Git Bash does.

set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]

image     := "pyvised-backend"
dev_image := image + ":dev"
# the published image; the app pulls the tag of its own version
registry  := "ghcr.io/zsomen2/pyvised-backend"
# app, backend and protocol share one version (`just set-version`); tags are v<version>
version   := `uv version --short --package pyvised`
# Windows wants four numbers: 1.0.0rc1 -> 1.0.0.0
numeric_version := replace_regex(version, '[^0-9.].*$', '') + ".0"
# folder with MCNP decks, outputs and xsdir for verify-rebuild (mounted at /decks)
decks     := env_var_or_default("PYVISED_DECKS", parent_directory(justfile_directory()) / "mcnp-hw")

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

# set the version of the app, backend and protocol, e.g. `just set-version 1.0.1`
set-version new:
    uv version {{new}} --package pyvised-protocol --no-sync
    uv version {{new}} --package pyvised-backend --no-sync
    uv version {{new}} --package pyvised

# build the backend image (OpenMC 0.16.0 from conda-forge + the PyVised service)
backend-build:
    docker build -f backend/Dockerfile --target runtime --build-arg VERSION={{version}} -t {{dev_image}} .

# the offline image file dist/pyvised-backend-<version>.tar from the local build, tagged as the
# release image so that "Load from file" accepts it (releases attach a gzipped one built by CI)
backend-save: backend-build
    docker tag {{dev_image}} {{registry}}:{{version}}
    New-Item -ItemType Directory -Force dist | Out-Null; docker save -o dist/pyvised-backend-{{version}}.tar {{registry}}:{{version}}

# run the backend and protocol tests inside the container, with real OpenMC
backend-test:
    docker build -f backend/Dockerfile --target test -t {{image}}:test .
    docker run --rm {{image}}:test

# check the model rebuilds against their MCNP decks: numbers, ID maps vs the MCNP adapter,
# overlaps, materials (local only: models/rebuilds.json lists them; optional: one name)
verify-rebuild name="":
    docker build -f backend/Dockerfile --target test -t {{image}}:test .
    docker run --rm -v "${PWD}\models:/models" -v "${PWD}\docs:/docs:ro" -v "{{decks}}:/decks:ro" {{image}}:test python -W ignore /models/verify_rebuild.py --manifest /models/rebuilds.json {{ if name == "" { "" } else { "--only " + name } }}

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

# the app folder dist/PyVised (PyInstaller, one folder, no console), then its self-test
build-exe: i18n-build
    uv run --group build pyinstaller --noconfirm --clean --distpath dist --workpath build/pyinstaller packaging/pyvised.spec
    just check-exe

# self-test of a built or installed PyVised.exe, away from your settings and a running PyVised;
# `just check-exe <exe> -Backend` checks Docker, the image and the backend too
check-exe exe="dist/PyVised/PyVised.exe" *options:
    ./packaging/check-exe.ps1 -Exe "{{exe}}" {{options}}; exit $LASTEXITCODE

# install the newest dist/PyVised-Setup-*.exe silently for you, self-test it, uninstall it
# (options: -Backend, -Keep)
check-install *options:
    ./installer/check-install.ps1 {{options}}

# the installer dist/PyVised-Setup-<version>.exe (Inno Setup 6: winget install JRSoftware.InnoSetup)
installer: build-exe
    uv run python packaging/wizard_images.py build/installer
    $iscc = @($env:ISCC, "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1; if (-not $iscc) { throw "Inno Setup 6 not found: winget install JRSoftware.InnoSetup" }; & $iscc /Qp "/DAppVersion={{version}}" "/DNumericVersion={{numeric_version}}" installer/pyvised.iss; exit $LASTEXITCODE
