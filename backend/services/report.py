"""Report Agent — assemble a PDF with summary, KPIs, chart, insight."""
from __future__ import annotations
import tempfile
from pathlib import Path


def build_report(question: str, state: dict) -> str:
    """Render a simple PDF report using reportlab. Returns the file path."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import cm

    df = state.get("df")
    out = Path(tempfile.gettempdir()) / "report.pdf"
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
