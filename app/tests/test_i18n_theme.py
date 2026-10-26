from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from pyvised.core.i18n import install_translators
from pyvised.core.paths import i18n_dir
from pyvised.core.settings import ThemeMode
from pyvised.core.theme import apply_theme, choose_style, is_dark


def test_hungarian_translation_is_built_and_loads(qapp):
    assert (i18n_dir() / "pyvised_hu.qm").is_file(), "run `just i18n-build`"
    translators = install_translators(qapp, "hu")
    try:
        assert QCoreApplication.translate("MainWindow", "Geometry") == "Geometria"
        assert QCoreApplication.translate("MainWindow", "Simulation") == "Szimuláció"
    finally:
        for translator in translators:
            QApplication.removeTranslator(translator)


def test_english_needs_no_translator(qapp):
    assert install_translators(qapp, "en") == []


def test_theme_modes_apply_without_errors(qapp):
    for mode in (ThemeMode.DARK, ThemeMode.LIGHT, ThemeMode.AUTO):
        apply_theme(mode)
        assert isinstance(is_dark(), bool)


def test_style_is_available(qapp):
    assert choose_style() in ("windows11", "Fusion")
