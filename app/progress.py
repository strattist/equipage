"""Progress data from structured logs and benchmarks, statistics for the coach's
encouragement, and the PNG chart posted in #coach. Never uses the model's memory."""

import io
from dataclasses import dataclass, field
from datetime import date, timedelta

from app import db

PANEL_BENCHMARK = "benchmark rameur"
PANEL_HIIT = "HIIT effort/repos"
PANEL_ROWS = "distance rameur"

# Reference palette (dataviz skill), validated light-mode pair: blue, orange.
SURFACE = "#ffffff"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
GRID = "#e4e3df"
BLUE = "#2a78d6"
ORANGE = "#eb6834"

MAX_RATIO_LABELS = 12


@dataclass
class ProgressData:
    benchmarks: list[tuple[date, int, float | None]] = field(default_factory=list)
    hiit: list[tuple[date, int, int, int]] = field(default_factory=list)
    rows: list[tuple[date, int, int | None]] = field(default_factory=list)
    completed_days: list[date] = field(default_factory=list)


def load_progress() -> ProgressData:
    with db.connect() as conn:
        bench_rows = conn.execute(
            "SELECT date, distance_m, avg_split_s FROM benchmarks ORDER BY date, id"
        ).fetchall()
        hiit_rows = conn.execute(
            "SELECT date, efforts, effort_s, rest_s FROM session_logs "
            "WHERE completed = 1 AND effort_s IS NOT NULL AND rest_s IS NOT NULL ORDER BY date, id"
        ).fetchall()
        row_rows = conn.execute(
            "SELECT date, distance_m, rpe FROM session_logs "
            "WHERE completed = 1 AND distance_m IS NOT NULL "
            "AND date NOT IN (SELECT date FROM benchmarks) ORDER BY date, id"
        ).fetchall()
        days = conn.execute(
            "SELECT date FROM session_logs WHERE completed = 1 ORDER BY date"
        ).fetchall()

    # A benchmark recorded twice on the same day is a correction: keep the latest.
    by_day: dict[str, tuple] = {}
    for r in bench_rows:
        by_day[r["date"]] = (
            date.fromisoformat(r["date"]),
            r["distance_m"],
            r["avg_split_s"],
        )
    return ProgressData(
        benchmarks=sorted(by_day.values()),
        hiit=[
            (date.fromisoformat(r["date"]), r["efforts"], r["effort_s"], r["rest_s"])
            for r in hiit_rows
        ],
        rows=[
            (date.fromisoformat(r["date"]), r["distance_m"], r["rpe"]) for r in row_rows
        ],
        completed_days=[date.fromisoformat(r["date"]) for r in days],
    )


def missing_panels(data: ProgressData) -> list[str]:
    panels = [
        (PANEL_BENCHMARK, data.benchmarks),
        (PANEL_HIIT, data.hiit),
        (PANEL_ROWS, data.rows),
    ]
    return [name for name, points in panels if len(points) < 2]


def _ratio(effort_s: int, rest_s: int) -> float:
    return round(effort_s / rest_s, 2)


def summarize(data: ProgressData) -> dict:
    summary: dict = {}
    if len(data.benchmarks) >= 2:
        (_, first_m, first_split), (_, latest_m, latest_split) = (
            data.benchmarks[0],
            data.benchmarks[-1],
        )
        bench = {
            "first_m": first_m,
            "latest_m": latest_m,
            "delta_m": latest_m - first_m,
            "delta_pct": round((latest_m - first_m) / first_m * 100, 1),
        }
        if first_split is not None and latest_split is not None:
            bench |= {
                "first_split_s": first_split,
                "latest_split_s": latest_split,
                "delta_split_s": round(latest_split - first_split, 1),
            }
        summary["benchmark"] = bench
    if len(data.hiit) >= 2:
        (_, fe, fes, frs), (_, le, les, lrs) = data.hiit[0], data.hiit[-1]
        summary["hiit"] = {
            "first_ratio": _ratio(fes, frs),
            "latest_ratio": _ratio(les, lrs),
            "first": {"efforts": fe, "effort_s": fes, "rest_s": frs},
            "latest": {"efforts": le, "effort_s": les, "rest_s": lrs},
        }
    count = 0
    if data.completed_days:
        latest = max(data.completed_days)
        since = latest - timedelta(days=28)
        count = sum(1 for d in data.completed_days if since < d <= latest)
    summary["sessions_last_28_days"] = count
    return summary


def _style(ax, title: str, ylabel: str) -> None:
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", color=INK, fontsize=12, fontweight="bold", pad=18)
    ax.set_ylabel(ylabel, color=INK_SECONDARY, fontsize=10)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_SECONDARY, labelsize=9, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _line_panel(ax, days, values, title, ylabel, annotate=None) -> None:
    import matplotlib.dates as mdates

    _style(ax, title, ylabel)
    ax.plot(
        days,
        values,
        color=BLUE,
        linewidth=2,
        marker="o",
        markersize=6,
        markeredgecolor=SURFACE,
        markeredgewidth=1.5,
        zorder=3,
    )
    if len(days) <= 8:
        ax.set_xticks(days)  # few points: label each measurement date
    else:
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=8))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    ax.margins(x=0.08, y=0.25)
    if annotate:
        ax.annotate(
            annotate,
            (days[-1], values[-1]),
            textcoords="offset points",
            xytext=(0, 10),
            ha="right",
            va="bottom",
            color=INK,
            fontsize=10,
            fontweight="bold",
        )


def _hiit_panel(ax, hiit) -> None:
    from matplotlib.ticker import MaxNLocator

    _style(ax, "HIIT : temps d'effort vs repos (secondes)", "secondes")
    n = len(hiit)
    xs = list(range(n))
    width = 0.38
    efforts = [h[2] for h in hiit]
    rests = [h[3] for h in hiit]
    bar = {"width": width, "edgecolor": SURFACE, "linewidth": 1, "zorder": 3}
    ax.bar([x - width / 2 for x in xs], efforts, color=BLUE, label="effort", **bar)
    ax.bar([x + width / 2 for x in xs], rests, color=ORANGE, label="repos", **bar)

    labelled = xs if n <= MAX_RATIO_LABELS else [0, n - 1]
    for x in labelled:
        top = max(efforts[x], rests[x])
        ax.annotate(
            f"{_ratio(efforts[x], rests[x]):.2f}".replace(".", ","),
            (x, top),
            textcoords="offset points",
            xytext=(0, 3),
            ha="center",
            va="bottom",
            color=INK_SECONDARY,
            fontsize=8,
        )

    if n <= MAX_RATIO_LABELS:
        ticks = xs
    else:
        located = MaxNLocator(nbins=8, integer=True).tick_values(0, n - 1)
        ticks = sorted({round(t) for t in located if 0 <= t <= n - 1} | {n - 1})
    ax.set_xticks(ticks, [hiit[t][0].strftime("%d/%m") for t in ticks])
    ax.set_ylim(0, max(efforts + rests) * 1.2)
    ax.legend(
        loc="lower right",
        bbox_to_anchor=(1, 1),
        frameon=False,
        ncols=2,
        fontsize=9,
        labelcolor=INK_SECONDARY,
        borderaxespad=0.2,
    )
    ax.text(
        0,
        1.0,
        "chiffre au-dessus : ratio effort/repos",
        transform=ax.transAxes,
        ha="left",
        va="bottom",
        color=INK_SECONDARY,
        fontsize=8,
    )


def render_png(data: ProgressData) -> bytes | None:
    panels = []
    if len(data.benchmarks) >= 2:
        panels.append("benchmark")
    if len(data.hiit) >= 2:
        panels.append("hiit")
    if len(data.rows) >= 2:
        panels.append("rows")
    if not panels:
        return None

    import matplotlib

    matplotlib.use("Agg")
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    fig = Figure(
        figsize=(8, 3.2 * len(panels)), dpi=150, facecolor=SURFACE, layout="constrained"
    )
    FigureCanvasAgg(fig)
    axes = fig.subplots(len(panels), 1, squeeze=False)[:, 0]
    for ax, panel in zip(axes, panels):
        if panel == "benchmark":
            days = [b[0] for b in data.benchmarks]
            meters = [b[1] for b in data.benchmarks]
            gain = meters[-1] - meters[0]
            note = f"{gain:+d} m depuis le {days[0].strftime('%d/%m')}"
            _line_panel(
                ax,
                days,
                meters,
                "Benchmark rameur 20 min (RPE 5)",
                "mètres",
                annotate=note,
            )
        elif panel == "hiit":
            _hiit_panel(ax, data.hiit)
        else:
            _line_panel(
                ax,
                [r[0] for r in data.rows],
                [r[1] for r in data.rows],
                "Distance par séance de rameur (m)",
                "mètres",
            )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=SURFACE)
    return buf.getvalue()
