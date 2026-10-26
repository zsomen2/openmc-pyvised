# PyInstaller spec of the PyVised desktop app: one windowed folder, dist/PyVised/PyVised.exe.
# `just build-exe` compiles the translations and runs it; installer/pyvised.iss packs the folder.
# PyInstaller provides Analysis, PYZ, EXE, COLLECT and SPECPATH.

import importlib.metadata
import re
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

ROOT = Path(SPECPATH).parent
APP = ROOT / "app"
PACKAGE = APP / "src" / "pyvised"
VERSION = importlib.metadata.version("pyvised")
NUMBERS = (*(int(n) for n in re.findall(r"\d+", VERSION)[:3]), 0)
"""Windows wants four numbers; a pre-release (1.0.0rc1) gets those of its final version."""

TRANSLATION = PACKAGE / "i18n" / "pyvised_hu.qm"
if not TRANSLATION.is_file():
    raise SystemExit(f"{TRANSLATION} is missing: run `just i18n-build` first")

# Optional imports of the libraries that PyVised does not need; leaving them out keeps
# their Qt libraries (data visualisation, test, network) out of the build. QtOpenGL and
# QtOpenGLWidgets stay: pyqtgraph imports them unconditionally.
EXCLUDES = [
    "PySide6.QtDataVisualization",  # qtpy (qtawesome) probes for it
    "PySide6.QtNetwork",
    "PySide6.QtTest",  # pyqtgraph's test helpers
    "click",  # httpx's command line
    "pygments",
    "tkinter",
]

# Qt plugins PyVised has no use for, and the libraries only they need: the software OpenGL
# renderer, the virtual keyboard (Qt Quick and QML), PDF images, network and TLS backends,
# touch input, and the Direct2D and minimal platforms.
UNUSED_BINARIES = {
    "opengl32sw.dll",
    "Qt6Network.dll",
    "Qt6Pdf.dll",
    "Qt6Qml.dll",
    "Qt6QmlMeta.dll",
    "Qt6QmlModels.dll",
    "Qt6QmlWorkerScript.dll",
    "Qt6Quick.dll",
    "Qt6VirtualKeyboard.dll",
    "qtvirtualkeyboardplugin.dll",
    "qpdf.dll",
    "qnetworklistmanager.dll",
    "qcertonlybackend.dll",
    "qopensslbackend.dll",
    "qschannelbackend.dll",
    "qtuiotouchplugin.dll",
    "qdirect2d.dll",
    "qminimal.dll",
}
QT_TRANSLATIONS = {"qtbase_hu.qm"}
"""Qt's own translations PyVised loads (English needs none)."""

# license texts of the bundled packages (their dist-info folders)
LICENSED = [
    "pyvised",
    "pyvised-protocol",
    "PySide6-Essentials",
    "shiboken6",
    "pyqtgraph",
    "numpy",
    "httpx",
    "httpcore",
    "h11",
    "anyio",
    "idna",
    "certifi",
    "pydantic",
    "pydantic-core",
    "qtawesome",
    "QtPy",
]

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=NUMBERS, prodvers=NUMBERS),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040904B0",
                    [
                        StringStruct("CompanyName", "zsomen2"),
                        StringStruct("FileDescription", "PyVised"),
                        StringStruct("FileVersion", VERSION),
                        StringStruct("InternalName", "PyVised"),
                        StringStruct("LegalCopyright", "GNU GPL v3 or later"),
                        StringStruct("OriginalFilename", "PyVised.exe"),
                        StringStruct("ProductName", "PyVised"),
                        StringStruct("ProductVersion", VERSION),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

datas = [
    (str(APP / "assets"), "assets"),
    (str(TRANSLATION), "pyvised/i18n"),
    (str(ROOT / "LICENSE"), "."),
]
for distribution in LICENSED:
    datas += copy_metadata(distribution)

a = Analysis(
    [str(PACKAGE / "__main__.py")],
    pathex=[str(APP / "src")],
    datas=datas,
    excludes=EXCLUDES,
)
a.binaries = [entry for entry in a.binaries if Path(entry[0]).name not in UNUSED_BINARIES]
a.datas = [
    entry
    for entry in a.datas
    if Path(entry[0]).parent.name != "translations" or Path(entry[0]).name in QT_TRANSLATIONS
]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PyVised",
    icon=str(APP / "assets" / "pyvised_app_icon.ico"),
    version=version_info,
    console=False,
    upx=False,  # UPX-packed files trip antivirus heuristics more often
)
coll = COLLECT(exe, a.binaries, a.datas, name="PyVised", upx=False)
