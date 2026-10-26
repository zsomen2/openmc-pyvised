"""Summary of one run: k-eff and its estimators, reactivity (pcm, and $ with the sidecar's
β_eff), the comparison with the model's reference results, the tally totals (normalised),
settings, timings and warnings."""

import html
from datetime import datetime

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QTextBrowser, QVBoxLayout, QWidget

from pyvised_protocol.runs import RunInfo, RunResults
from pyvised_protocol.sidecar import Sidecar

from ...render import tallies as tm
from .live_plots import format_duration, group_digits

QUALITY_COLORS = {
    tm.Quality.GOOD: "#1a8f3a",
    tm.Quality.FAIR: "#c77c00",
    tm.Quality.POOR: "#c62828",
    tm.Quality.NONE: "",
}


def local_time(value: datetime | None) -> str:
    if value is None:
        return "–"
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def heading(text: str) -> str:
    return f"<h4>{html.escape(text)}</h4>"


def k_text(value: float, std: float | None) -> str:
    return f"{value:.5f} ± {std:.5f}" if std is not None else f"{value:.5f}"


class SummaryView(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.browser)
        self.language = "en"
        self._shown: tuple[str | None, str] = ("", "")
        """Run id and HTML on screen."""
        self.show_run(None, None, None)

    def state_text(self, run: RunInfo) -> str:
        states = {
            "preparing": self.tr("preparing"),
            "running": self.tr("running"),
            "summarising": self.tr("reading the results"),
            "finished": self.tr("finished"),
            "stopped": self.tr("stopped"),
            "failed": self.tr("failed"),
        }
        reasons = {
            "completed": self.tr("all batches done"),
            "triggers": self.tr("the targets were reached"),
            "user": self.tr("stopped by the user"),
            "time_limit": self.tr("time limit reached"),
            "interrupted": self.tr("the backend stopped during the run"),
            "error": self.tr("OpenMC stopped with an error"),
        }
        text = states.get(run.state, run.state)
        if run.stop_reason:
            text += f" ({reasons.get(run.stop_reason, run.stop_reason)})"
        return text

    def show_run(
        self, run: RunInfo | None, results: RunResults | None, sidecar: Sidecar | None
    ) -> None:
        if run is None:
            self._show(
                None,
                "<p>" + html.escape(self.tr("Start a run, or pick one in the history.")) + "</p>",
            )
            return
        muted = self.palette().color(QPalette.ColorRole.PlaceholderText).name()
        parts = [f"<h3>{html.escape(run.label or run.path.rsplit('/', 1)[-1])}</h3>"]
        rows = [
            (self.tr("State"), html.escape(self.state_text(run))),
            (self.tr("Folder"), html.escape(run.path)),
            (self.tr("Started"), local_time(run.started or run.created)),
        ]
        if run.finished:
            rows.append((self.tr("Run time"), format_duration(run.seconds)))
        plan = run.plan
        if plan is not None:
            size = self.tr("{particles} particles × {batches} batches").format(
                particles=group_digits(plan.particles), batches=plan.batches
            )
            if plan.inactive:
                size += " " + self.tr("({inactive} inactive)").format(inactive=plan.inactive)
            rows.append((self.tr("Size"), size))
            if plan.triggers:
                rows.append((self.tr("Targets"), html.escape(", ".join(plan.triggers))))
            rows.append((self.tr("Threads"), str(plan.threads)))
            if plan.seed is not None:
                rows.append((self.tr("Seed"), str(plan.seed)))
        if run.params:
            text = ", ".join(f"{k} = {v:g}" for k, v in run.params.items())
            rows.append((self.tr("Parameters"), html.escape(text)))
        if run.library_name:
            rows.append((self.tr("Library"), html.escape(run.library_name)))
        if run.versions.get("openmc"):
            rows.append(("OpenMC", html.escape(run.versions["openmc"])))
        parts.append(self._table(rows))
        if run.error:
            parts.append(f"<pre style='color:#c62828'>{html.escape(run.error)}</pre>")
        if results is None:
            if run.active:
                note = html.escape(self.tr("Results follow when the run ends."))
                parts.append(f"<p style='color:{muted}'>{note}</p>")
            elif not run.has_results:
                parts.append("<p>" + html.escape(self.tr("The run left no results.")) + "</p>")
                first = run.plan.statepoints[0] if run.plan and run.plan.statepoints else None
                stopped = run.stop_reason in ("user", "time_limit")
                if stopped and first is not None and run.batches_done < first:
                    note = self.tr(
                        "It stopped at batch {batch}, before its first statepoint (batch {first})."
                    ).format(batch=run.batches_done, first=first)
                    parts.append(f"<p>{html.escape(note)}</p>")
            self._show(run.id, "".join(parts))
            return
        parts += self._keff(results, run, sidecar)
        parts += self._tallies(results, run, sidecar)
        parts += self._performance(results)
        warnings = run.warnings + results.warnings
        if warnings:
            parts.append(heading(self.tr("Warnings")) + "<ul>")
            parts += [f"<li>{html.escape(w)}</li>" for w in warnings]
            parts.append("</ul>")
        self._show(run.id, "".join(parts))

    def _show(self, run_id: str | None, page: str) -> None:
        """Show `page`; the same run again keeps the reader's place (refreshes come often)."""
        if (run_id, page) == self._shown:
            return
        bar = self.browser.verticalScrollBar()
        position = bar.value() if run_id == self._shown[0] else 0
        self._shown = (run_id, page)
        self.browser.setHtml(page)
        bar.setValue(position)

    @staticmethod
    def _table(rows: list[tuple[str, str]]) -> str:
        cells = "".join(
            f"<tr><td style='padding-right:14px'>{html.escape(name)}</td><td>{value}</td></tr>"
            for name, value in rows
        )
        return f"<table cellspacing='0' cellpadding='2'>{cells}</table>"

    def _keff(self, results: RunResults, run: RunInfo, sidecar: Sidecar | None) -> list[str]:
        keff = results.keff
        if keff is None or keff.combined is None:
            if results.leakage is not None:
                leakage = k_text(results.leakage.value, results.leakage.std)
                return [
                    heading(self.tr("Leakage")),
                    self._table([(self.tr("Leakage fraction"), leakage)]),
                ]
            return []
        k, std = keff.combined.value, keff.combined.std
        rho = tm.reactivity(k)
        rho_std = tm.reactivity_std(k, std) if std is not None else None
        rows = [(self.tr("k-eff (combined)"), f"<b>{k_text(k, std)}</b>")]
        reactivity = f"{tm.pcm(rho):.0f}"
        if rho_std is not None:
            reactivity += f" ± {tm.pcm(rho_std):.0f}"
        reactivity += " pcm"
        beta = sidecar.beta_eff if sidecar else None
        if beta:
            dollars = rho / beta
            reactivity += f" = {dollars:.3f}"
            if rho_std is not None:
                reactivity += f" ± {rho_std / beta:.3f}"
            reactivity += " $"
            reactivity += " " + self.tr("(β_eff = {beta:g})").format(beta=beta)
        rows.append((self.tr("Reactivity ρ"), reactivity))
        for name, value in (
            (self.tr("collision"), keff.collision),
            (self.tr("track-length"), keff.track_length),
            (self.tr("absorption"), keff.absorption),
        ):
            if value is not None:
                rows.append((f"  {name}", k_text(value.value, value.std)))
        if results.leakage is not None:
            rows.append(
                (self.tr("Leakage fraction"), k_text(results.leakage.value, results.leakage.std))
            )
        parts = [heading("k-eff"), self._table(rows)]
        references = [
            r
            for r in tm.matching(sidecar.references if sidecar else [], run.params)
            if r.tally is None
        ]
        if references:
            parts.append(self._references(references, k, std, beta))
        return parts

    def _references(self, references, value: float, std: float | None, beta: float | None) -> str:
        titles = (
            self.tr("Reference"),
            self.tr("Value"),
            self.tr("Difference"),
            self.tr("Agreement"),
        )
        head = "<tr>" + "".join(f"<th align='left'>{html.escape(t)}</th>" for t in titles) + "</tr>"
        body = []
        for reference in references:
            c = tm.compare(reference, value, std)
            ref_std = tm.reference_std(reference)
            difference = f"{tm.pcm(c.difference):+.0f} pcm"
            if beta:
                rho = tm.reactivity(value) - tm.reactivity(reference.value)
                difference += f" ({rho / beta:+.3f} $ " + html.escape(self.tr("in ρ")) + ")"
            body.append(
                self._comparison_row(
                    reference.name,
                    reference.source,
                    k_text(reference.value, ref_std),
                    difference,
                    c,
                )
            )
        return f"<table cellspacing='0' cellpadding='3'>{head}{''.join(body)}</table>"

    def _comparison_row(self, name: str, source: str, value: str, difference: str, c) -> str:
        tolerance = c.reference.tolerance
        if tolerance is not None:
            limit = (
                f"{tm.pcm(tolerance):.0f} pcm"
                if c.reference.tally is None
                else f"{100 * tolerance:g} %"
            )
            text = (self.tr("within ±{limit}") if c.agrees else self.tr("outside ±{limit}")).format(
                limit=limit
            )
            if c.sigmas is not None:
                text += f" ({c.sigmas:.1f} σ)"
        elif c.sigmas is not None:
            text = self.tr("{sigmas:.1f} σ").format(sigmas=c.sigmas)
        else:
            text = ""
        if not text:
            verdict = "–"
        else:
            color = "#1a8f3a" if c.agrees else "#c62828"
            verdict = f"<span style='color:{color}'>{html.escape(text)}</span>"
        title = html.escape(name) + (f"<br><small>{html.escape(source)}</small>" if source else "")
        return f"<tr><td>{title}</td><td>{value}</td><td>{difference}</td><td>{verdict}</td></tr>"

    def _tallies(self, results: RunResults, run: RunInfo, sidecar: Sidecar | None) -> list[str]:
        if not results.tallies:
            return []
        seconds = results.active_seconds
        rows = []
        normalisations = sidecar.tallies if sidecar else {}
        references = tm.matching(sidecar.references if sidecar else [], run.params)
        compared = []
        for tally in results.tallies:
            norm = normalisations.get(tally.id)
            scale = tm.factor(norm)
            unit = norm.unit if norm is not None else ""
            for nuclide, score, total in tm.totals(tally):
                rel = tm.rel_err(total.value, total.std)
                q = tm.quality(rel)
                color = QUALITY_COLORS[q]
                rel_text = "–" if rel is None else f"{100 * rel:.2f} %"
                if color:
                    rel_text = f"<span style='color:{color}'>{rel_text}</span>"
                fom = tm.figure_of_merit(rel, seconds)
                name = f"{tally.id} {tally.name}".strip()
                label = f"{score}" + (f" ({nuclide})" if nuclide != "total" else "")
                value = f"{total.value * scale:.5g}" + (f" {html.escape(unit)}" if unit else "")
                rows.append(
                    f"<tr><td>{html.escape(name)}</td><td>{html.escape(label)}</td><td>{value}</td>"
                    f"<td>{rel_text}</td><td>{'–' if fom is None else f'{fom:.3g}'}</td></tr>"
                )
                for reference in references:
                    if reference.tally == tally.id and nuclide == "total":
                        std = total.std * scale if total.std is not None else None
                        compared.append((reference, total.value * scale, std, unit))
        head = "".join(
            f"<th align='left'>{html.escape(t)}</th>"
            for t in (
                self.tr("Tally"),
                self.tr("Score"),
                self.tr("Total"),
                self.tr("Rel. error"),
                "FOM",
            )
        )
        parts = [
            heading(self.tr("Tallies")),
            f"<table cellspacing='0' cellpadding='3'><tr>{head}</tr>{''.join(rows)}</table>",
            "<p><small>"
            + html.escape(
                self.tr(
                    "Totals over all bins; their σ treats the bins as independent. "
                    "FOM = 1/(R²T), T in minutes of active batches."
                )
            )
            + "</small></p>",
        ]
        if compared:
            body = []
            for reference, value, std, unit in compared:
                c = tm.compare(reference, value, std)
                ref_std = tm.reference_std(reference)
                ref_text = f"{reference.value:.5g}" + (f" ± {ref_std:.2g}" if ref_std else "")
                body.append(
                    self._comparison_row(
                        reference.name,
                        reference.source,
                        f"{ref_text} {html.escape(unit)}",
                        f"{100 * c.difference:+.1f} %",
                        c,
                    )
                )
            parts.append(f"<table cellspacing='0' cellpadding='3'>{''.join(body)}</table>")
        return parts

    def _performance(self, results: RunResults) -> list[str]:
        rows = [(self.tr("Statepoint"), html.escape(f"{results.statepoint} ({results.batches})"))]
        if results.rate_active:
            rows.append((self.tr("Rate (active)"), group_digits(results.rate_active) + " /s"))
        if results.rate_inactive:
            rows.append((self.tr("Rate (inactive)"), group_digits(results.rate_inactive) + " /s"))
        total = results.runtime.get("total")
        if total:
            rows.append((self.tr("OpenMC time"), format_duration(total)))
        return [heading(self.tr("Performance")), self._table(rows)]
