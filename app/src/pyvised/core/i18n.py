"""Translations: the app's Qt Linguist files plus Qt's own `qtbase_<lang>.qm`.

The default locale is left alone on purpose: numeric fields keep the dot as the
decimal separator in every language, matching OpenMC inputs.
"""

import logging

from PySide6.QtCore import QCoreApplication, QLibraryInfo, QTranslator

from .paths import i18n_dir

log = logging.getLogger(__name__)


def install_translators(app: QCoreApplication, language: str) -> list[QTranslator]:
    """Install the translators for `language` ("en" needs none); keep the result alive."""
    if language == "en":
        return []
    installed = []
    own = QTranslator(app)
    if own.load(f"pyvised_{language}", str(i18n_dir())):
        app.installTranslator(own)
        installed.append(own)
    else:
        log.warning("no translation file pyvised_%s.qm in %s", language, i18n_dir())
    qt = QTranslator(app)
    qt_dir = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if qt.load(f"qtbase_{language}", qt_dir):
        app.installTranslator(qt)
        installed.append(qt)
    return installed
