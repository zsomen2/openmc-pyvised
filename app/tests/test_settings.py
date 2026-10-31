import os
from pathlib import Path

from PySide6.QtCore import QSettings

from pyvised.core.paths import default_workspace
from pyvised.core.settings import MAX_RECENT, AppSettings, PlotBackground, ReloadPolicy, ThemeMode


def test_defaults(settings: AppSettings):
    assert settings.language == "en"
    assert settings.theme is ThemeMode.AUTO
    assert settings.workspace == default_workspace()
    assert settings.ask_workspace_at_startup is False
    assert settings.backend_image == ""
    assert settings.keep_backend_running is False
    assert settings.backend_cpus == 0.0
    assert settings.reopen_last_model is True
    assert settings.reload_on_change is ReloadPolicy.ASK
    assert settings.recent_models == []
    assert settings.render_quality == 1.0
    assert settings.plot_background is PlotBackground.THEME
    assert (settings.color_mode, settings.show_lines) == ("material", True)
    assert (settings.line_mode, settings.line_width) == ("cell_instance", 1)
    assert settings.default_views == ("xz", "xy")


def test_values_survive_a_reload(tmp_path: Path):
    path = str(tmp_path / "PyVised.ini")
    first = AppSettings(QSettings(path, QSettings.Format.IniFormat))
    first.language = "hu"
    first.theme = ThemeMode.DARK
    first.workspace = tmp_path / "work"
    first.ask_workspace_at_startup = True
    first.backend_image = " pyvised-backend:dev "
    first.keep_backend_running = True
    first.backend_cpus = 4
    first.reopen_last_model = False
    first.reload_on_change = ReloadPolicy.ALWAYS
    first.render_quality = 0.7
    first.plot_background = PlotBackground.WHITE
    first.color_mode = "none"
    first.show_lines = False
    first.line_mode = "material"
    first.line_width = 2
    first.default_views = ("yz", "xy")
    first.add_recent_model(tmp_path / "a.py")
    first.sync()

    second = AppSettings(QSettings(path, QSettings.Format.IniFormat))
    assert second.language == "hu"
    assert second.theme is ThemeMode.DARK
    assert second.workspace == tmp_path / "work"
    assert second.ask_workspace_at_startup is True
    assert second.backend_image == "pyvised-backend:dev"
    assert second.keep_backend_running is True
    assert second.backend_cpus == 4.0
    assert second.reopen_last_model is False
    assert second.reload_on_change is ReloadPolicy.ALWAYS
    assert second.render_quality == 0.7
    assert second.plot_background is PlotBackground.WHITE
    assert (second.color_mode, second.show_lines) == ("none", False)
    assert (second.line_mode, second.line_width) == ("material", 2)
    assert second.default_views == ("yz", "xy")
    assert second.recent_models == [tmp_path / "a.py"]


def test_invalid_values_fall_back(settings: AppSettings):
    settings.language = "de"
    assert settings.language == "en"
    settings._store.setValue("general/theme", "purple")
    assert settings.theme is ThemeMode.AUTO
    settings._store.setValue("backend/cpus", "many")
    assert settings.backend_cpus == 0.0
    settings._store.setValue("geometry/render_quality", 3)
    assert settings.render_quality == 1.0
    settings._store.setValue("geometry/default_views", "xy,zz")
    assert settings.default_views == ("xz", "xy")
    settings._store.setValue("general/reload_on_change", "sometimes")
    assert settings.reload_on_change is ReloadPolicy.ASK
    assert settings.input_font_size == 10
    settings._store.setValue("input/font_size", "huge")
    assert settings.input_font_size == 10
    settings.input_font_size = 300
    assert settings.input_font_size == 40


def test_recent_models_are_newest_first_without_duplicates(settings: AppSettings, tmp_path: Path):
    for name in ("a", "b", "a"):
        settings.add_recent_model(tmp_path / f"{name}.py")
    assert settings.recent_models == [tmp_path / "a.py", tmp_path / "b.py"]
    for i in range(MAX_RECENT + 3):
        settings.add_recent_model(tmp_path / f"m{i}.py")
    assert len(settings.recent_models) == MAX_RECENT
    settings.clear_recent_models()
    assert settings.recent_models == []


def test_simulation_settings_and_added_libraries(tmp_path: Path):
    path = str(tmp_path / "PyVised.ini")
    first = AppSettings(QSettings(path, QSettings.Format.IniFormat))
    assert first.run_threads == 0 and first.run_library == "" and first.run_entropy_mesh
    assert first.run_limits == {
        "time_limit": (False, 10.0),
        "keff_std": (False, 50.0),
        "tally_rel_err": (False, 5.0),
    }
    first.run_threads = 8
    first.set_run_limit("time_limit", True, 2.5)
    first.added_libraries = [("endfb80", Path("D:/data/endfb-viii.0"))]
    first.sync()
    again = AppSettings(QSettings(path, QSettings.Format.IniFormat))
    assert again.run_threads == 8
    assert again.run_limits["time_limit"] == (True, 2.5)
    assert again.added_libraries == [("endfb80", Path("D:/data/endfb-viii.0"))]


def test_backend_image_housekeeping(settings: AppSettings):
    assert settings.remove_old_images is True and settings.selftest_image == ""
    settings.remove_old_images = False
    settings.selftest_image = " sha256:abc "
    assert settings.remove_old_images is False and settings.selftest_image == "sha256:abc"


def test_machine_defaults_apply_until_the_user_changes_them(tmp_path: Path, monkeypatch):
    r"""The installer's "shared lab PC" option writes %PROGRAMDATA%\PyVised\PyVised.ini;
    Qt reads it for keys the user's own file does not have."""
    user, system = tmp_path / "roaming", tmp_path / "programdata"
    (system / "PyVised").mkdir(parents=True)
    (system / "PyVised" / "PyVised.ini").write_text(
        "[%General]\nask_workspace_at_startup=true\nreopen_last_model=false\n", encoding="utf-8"
    )
    ini, user_scope, system_scope = (
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        QSettings.Scope.SystemScope,
    )
    QSettings.setPath(ini, user_scope, str(user))
    QSettings.setPath(ini, system_scope, str(system))
    try:
        settings = AppSettings()
        assert settings.ask_workspace_at_startup is True
        assert settings.reopen_last_model is False
        settings.reopen_last_model = True
        settings.sync()
        assert AppSettings().reopen_last_model is True
        assert (user / "PyVised" / "PyVised.ini").is_file(), "the user's own file"
    finally:
        QSettings.setPath(ini, user_scope, os.environ.get("APPDATA", ""))
        QSettings.setPath(ini, system_scope, os.environ.get("PROGRAMDATA", ""))
