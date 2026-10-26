# Deploying PyVised on lab PCs

PyVised is a Windows desktop app that runs OpenMC in a Docker container it manages itself.
A lab PC needs Docker Desktop, PyVised, and the OpenMC backend image (about 1 GB to download,
3.4 GB on disk), which PyVised downloads at its first start. Docker keeps images per Windows
user, so the image is downloaded once for each account that uses PyVised; on a lab with one
shared account, that is once per PC.

## Checklist

| Done | Item | How to check |
|---|---|---|
| ☐ | Windows 10 22H2 (build 19045) or Windows 11 23H2 or later, 64-bit, Pro, Education or Enterprise | `winver` |
| ☐ | Virtualization enabled in the BIOS/UEFI | Task Manager › Performance › CPU: "Virtualization: Enabled" |
| ☐ | WSL 2.1.5 or later | `wsl --version`; update with `wsl --update` |
| ☐ | 16 GB RAM recommended, 8 GB at least | |
| ☐ | 10 GB free on the system drive | Docker's data, the image, the students' runs |
| ☐ | Docker Desktop installed for all users, its licence accepted, its version pinned | [step 1](#1-docker-desktop) |
| ☐ | PyVised installed for all users with the lab option | [step 2](#2-pyvised) |
| ☐ | HTTPS to `ghcr.io` and `pkg-containers.githubusercontent.com`, or the offline image file | [step 3](#3-the-backend-image) |
| ☐ | As the lab user: the self-test passes | [step 4](#4-check-the-installation) |
| ☐ | The Hungarian user interface and the workspace prompt look right | start PyVised as the lab user |

## 1. Docker Desktop

Install it for all users, as an administrator:

```bat
"Docker Desktop Installer.exe" install --quiet --accept-license --always-run-service
```

- `--accept-license` accepts the Docker Subscription Service Agreement for the users. Docker
  Desktop is free for education.
- The default WSL 2 backend runs Linux containers; the users do not need to be in the
  `docker-users` group for that.
- Keep one Docker Desktop version on all lab PCs and turn off its automatic updates until a
  new version has been tried with PyVised.
- PyVised starts Docker Desktop when it is not running and waits up to three minutes for it.
  Letting Docker Desktop start at sign-in (Docker Desktop › Settings › General) saves that wait.

Docker's installation guide, with the other installer options:
https://docs.docker.com/desktop/setup/install/windows-install/

## 2. PyVised

Download `PyVised-Setup-<version>.exe` from the
[releases](https://github.com/zsomen2/openmc-pyvised/releases) and install it for all users,
silently, as an administrator:

```bat
PyVised-Setup-1.0.0.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /ALLUSERS /MERGETASKS="lab" /LANG=hu /LOG="C:\Windows\Temp\pyvised-setup.log"
```

- PyVised goes to `C:\Program Files\PyVised`, with a Start-menu shortcut for everyone. Add
  `desktopicon` to `/MERGETASKS` (`/MERGETASKS="lab,desktopicon"`) for desktop shortcuts.
- `lab` ("Shared lab PC") writes defaults for every user into
  `C:\ProgramData\PyVised\PyVised.ini`: PyVised asks for the workspace folder at each start
  and does not reopen the previous user's model. A user's own changes in Settings win.
- `/LANG=hu` makes Hungarian the default language of PyVised (the installer's language is
  the app's default); users can switch in Settings.
- The exit code is 0 when the installation succeeded; the log says what was done.
- **Update:** run the installer of the new version the same way. At its next start PyVised
  downloads the backend image of the new version and removes the old one.
- **Uninstall:** `"C:\Program Files\PyVised\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART`.
  Settings, workspaces and the backend image stay. Remove the image first in PyVised
  (Tools › Backend setup… › Remove from this PC…) or with
  `docker image rm ghcr.io/zsomen2/pyvised-backend:<version>` as the lab user.

More defaults can go into the same file, in its `[%General]` section (the `%` belongs to the
name), for example a workspace on another drive:

```ini
[%General]
ask_workspace_at_startup=true
reopen_last_model=false
language=hu
workspace=D:/PyVised
```

## 3. The backend image

- **Online:** start PyVised as the lab user. It downloads
  `ghcr.io/zsomen2/pyvised-backend:<version>` (about 1 GB) and runs a self-test. Only HTTPS to
  `ghcr.io` and `pkg-containers.githubusercontent.com` is needed.
- **Offline:** download `pyvised-backend-<version>.tar.gz` from the same release, then as the
  lab user choose Tools › Backend setup… › Load from file… in PyVised, or run
  `docker load -i pyvised-backend-<version>.tar.gz`.

## 4. Check the installation

As the lab user, with Docker Desktop installed:

```powershell
$p = Start-Process "C:\Program Files\PyVised\PyVised.exe" -ArgumentList "--self-test", "--report", "C:\Windows\Temp\pyvised-selftest.txt" -Wait -PassThru
$p.ExitCode
Get-Content C:\Windows\Temp\pyvised-selftest.txt
```

The self-test checks the app itself, Docker, the backend image (downloading it when it is
missing), starts the backend and builds, renders and runs a small test model. Exit code 0:
everything passed; 1: something failed (the report says what); 2: PyVised was running.
`--no-backend` checks only the app. PyVised's log is in `%LOCALAPPDATA%\PyVised\logs`.

## Shared accounts

- Everyone using the account sees the same files. With the workspace prompt, each student
  picks a folder of their own at start-up.
- The workspace must be on a local disk or a USB drive: Docker Desktop cannot mount network
  drives (mapped drive letters or `\\server\share` paths).
- PyVised stops its backend, and a simulation still running in it, when it exits, so nothing
  keeps the PC busy for the next user.

## Code signing and antivirus

- The installer and `PyVised.exe` are not code-signed yet. A silent installation by IT is not
  stopped by SmartScreen; files downloaded with a browser may need `Unblock-File` first.
- Antivirus programs occasionally flag apps built with PyInstaller. If that happens, allow
  `C:\Program Files\PyVised\` or the files by their SHA-256 hashes (the release's
  `SHA256SUMS.txt`).
- Smart App Control on Windows 11 (off on managed PCs) blocks unsigned apps.

## When something does not work

PyVised opens its setup window when something blocks the backend and says what to do. Its
*Copy diagnostics* button (also in the status bar's backend menu) copies the versions,
Docker's state and the backend log for a bug report.

| Symptom | Usual cause |
|---|---|
| "Docker is not running" | virtualization off in the BIOS/UEFI, WSL missing or old (`wsl --update`), Docker Desktop's licence not accepted |
| The download fails | a proxy or firewall blocks `ghcr.io`: use the offline image file |
| The self-test fails | the report and Copy diagnostics show where |
