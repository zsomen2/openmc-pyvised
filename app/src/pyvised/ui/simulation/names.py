"""Tally filter names in the UI language (`render.tallies` stays free of Qt)."""

from PySide6.QtCore import QCoreApplication

from pyvised_protocol.runs import TallyFilterResult

from ...render import tallies as tm


def filter_title(f: TallyFilterResult) -> str:
    # literal strings, so that lupdate finds them
    names = {
        "cell": QCoreApplication.translate("FilterNames", "cell"),
        "surface": QCoreApplication.translate("FilterNames", "surface"),
        "material": QCoreApplication.translate("FilterNames", "material"),
        "universe": QCoreApplication.translate("FilterNames", "universe"),
        "cellborn": QCoreApplication.translate("FilterNames", "birth cell"),
        "cellfrom": QCoreApplication.translate("FilterNames", "cell from"),
        "cellinstance": QCoreApplication.translate("FilterNames", "cell instance"),
        "distribcell": QCoreApplication.translate("FilterNames", "cell instance"),
        "energy": QCoreApplication.translate("FilterNames", "energy"),
        "energyout": QCoreApplication.translate("FilterNames", "outgoing energy"),
        "energyfunction": QCoreApplication.translate("FilterNames", "energy function"),
        "mesh": QCoreApplication.translate("FilterNames", "mesh"),
        "meshborn": QCoreApplication.translate("FilterNames", "birth mesh"),
        "meshsurface": QCoreApplication.translate("FilterNames", "mesh surface"),
        "particle": QCoreApplication.translate("FilterNames", "particle"),
        "time": QCoreApplication.translate("FilterNames", "time"),
        "mu": QCoreApplication.translate("FilterNames", "scattering cosine"),
        "polar": QCoreApplication.translate("FilterNames", "polar angle"),
        "azimuthal": QCoreApplication.translate("FilterNames", "azimuthal angle"),
        "delayedgroup": QCoreApplication.translate("FilterNames", "delayed group"),
    }
    return names.get(f.kind, f.kind)


def filter_summary(f: TallyFilterResult) -> str:
    title = filter_title(f)
    if f.kind in tm.ID_KINDS and f.ids:
        shown = ", ".join(str(i) for i in f.ids[:6])
        return f"{title} {shown}" + (" …" if len(f.ids) > 6 else "")
    if f.summary:
        return f"{title}: {f.summary}"
    return f"{title} ({f.bins})"
