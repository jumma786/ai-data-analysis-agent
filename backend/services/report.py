"""Report Agent — assemble a PDF with summary, KPIs, chart, insight.

The chart is rendered by exporting the Plotly figure to PNG via kaleido. That is
an optional dependency: if it is missing, or export fails, the report is still
produced with a short note in place of the image rather than failing outright. A
report without a picture is far better than no report.
"""
from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Any

from backend.utils.logging_config import logger


def render_chart_png(df: Any, chart: str, out_path: str | Path,
                     width: int = 900, height: int = 500) -> bool:
    """Write the figure for `df`/`chart` to `out_path` as PNG.

    Returns True on success, False if the figure could not be built or kaleido
    is unavailable. Never raises -- callers treat the chart as optional.
    """
    if df is None or not chart:
        return False
    try:
        from backend.agents.analysis_agents import build_figure

        fig = build_figure(df, chart)
        if fig is None:
            return False
        fig.write_image(str(out_path), width=width, height=height)
        return True
    except ImportError as exc:
        logger.info("Chart image skipped -- missing dependency: %s. "
                    "`pip install kaleido` to embed charts.", exc)
        return False
    except Exception as exc:  # noqa: BLE001 -- kaleido surfaces varied errors
        logger.warning("Chart image skipped -- export failed: %s", exc)
        return False


def build_report(question: str, state: dict) -> str:
    """Render a PDF report using reportlab. Returns the file path.

    Each call gets a unique filename. A fixed name here would mean concurrent
    requests from different users race on the same file on disk, and whoever's
    report is downloaded next -- once a download route exists -- may not be the
    same user who generated it.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.pdfgen import canvas

    df = state.get("df")
    report_id = uuid.uuid4().hex
    out = Path(tempfile.gettempdir()) / f"report-{report_id}.pdf"
    c = canvas.Canvas(str(out), pagesize=A4)
    w, h = A4
    y = h - 3 * cm

    c.setFont("Helvetica-Bold", 16)
    c.drawString(2 * cm, y, "AI Data Analysis Report")
    y -= 1 * cm
    c.setFont("Helvetica", 11)
    c.drawString(2 * cm, y, f"Question: {question[:90]}")
    y -= 0.8 * cm
    c.drawString(2 * cm, y, f"SQL: {(state.get('sql') or '')[:90]}")
    y -= 0.8 * cm
    if df is not None:
        c.drawString(2 * cm, y, f"Rows returned: {len(df)}")
        y -= 0.8 * cm

    # Chart, if one can be produced.
    chart_path = Path(tempfile.gettempdir()) / f"report-chart-{report_id}.png"
    if render_chart_png(df, state.get("chart", ""), chart_path):
        img_h = 8 * cm
        y -= img_h + 0.3 * cm
        c.drawImage(str(chart_path), 2 * cm, y, width=w - 4 * cm, height=img_h,
                    preserveAspectRatio=True, anchor="n")
        y -= 0.5 * cm
    elif state.get("chart"):
        c.setFont("Helvetica-Oblique", 9)
        c.drawString(2 * cm, y,
                     f"[chart '{state['chart']}' not embedded - install kaleido]")
        y -= 0.8 * cm

    insight = state.get("insight", "")
    c.setFont("Helvetica-Bold", 12)
    c.drawString(2 * cm, y, "Insight")
    y -= 0.7 * cm
    c.setFont("Helvetica", 10)
    for line in _wrap(insight, 90):
        c.drawString(2 * cm, y, line)
        y -= 0.55 * cm

    c.showPage()
    c.save()
    return str(out)


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for wd in words:
        if len(cur) + len(wd) + 1 > width:
            lines.append(cur); cur = wd
        else:
            cur = f"{cur} {wd}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]
