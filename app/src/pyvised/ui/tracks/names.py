"""Particle and event names in the UI language (`render.tracks` stays free of Qt)."""

from PySide6.QtCore import QCoreApplication

from pyvised_protocol.tracks import TrackEvent, particle_name


def event_name(event: TrackEvent | int) -> str:
    # literal strings, so that lupdate finds them
    names = {
        TrackEvent.SOURCE: QCoreApplication.translate("TrackNames", "source"),
        TrackEvent.SECONDARY: QCoreApplication.translate("TrackNames", "secondary particle"),
        TrackEvent.SPLIT: QCoreApplication.translate("TrackNames", "split copy"),
        TrackEvent.CROSSING: QCoreApplication.translate("TrackNames", "surface crossing"),
        TrackEvent.COLLISION: QCoreApplication.translate("TrackNames", "collision"),
        TrackEvent.REFLECTION: QCoreApplication.translate("TrackNames", "reflection"),
        TrackEvent.JUMP: QCoreApplication.translate("TrackNames", "periodic boundary"),
        TrackEvent.LEAKED: QCoreApplication.translate("TrackNames", "leaked"),
        TrackEvent.ABSORBED: QCoreApplication.translate("TrackNames", "absorbed"),
        TrackEvent.KILLED: QCoreApplication.translate("TrackNames", "killed (roulette or cutoff)"),
    }
    return names.get(TrackEvent(int(event)), str(event))


def particle_label(code: int) -> str:
    names = {
        "neutron": QCoreApplication.translate("TrackNames", "neutron"),
        "photon": QCoreApplication.translate("TrackNames", "photon"),
        "electron": QCoreApplication.translate("TrackNames", "electron"),
        "positron": QCoreApplication.translate("TrackNames", "positron"),
    }
    name = particle_name(code)
    return names.get(name, name)
