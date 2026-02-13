"""Generate PDF report for Odometer data."""
import io
import html
from datetime import datetime
from typing import Any, Dict, List, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _fmt_time(minutes: int) -> str:
    if minutes <= 0:
        return "0m"
    days = minutes // (60 * 24)
    hours = (minutes % (60 * 24)) // 60
    mins = minutes % 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{mins}m")
    return " ".join(parts)


def _fmt_iso(iso_str: Optional[str]) -> str:
    if not iso_str:
        return "-"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_str


def generate_report(
    vehicle_name: str,
    stats: Dict[str, Any],
    maintenance: List[Dict],
    thrusters: Dict[str, Any],
    accessories: List[Dict],
    missions: List[Dict],
    current_mission: Optional[Dict],
) -> bytes:
    """Generate a PDF report and return as bytes."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=inch,
        leftMargin=inch,
        topMargin=inch,
        bottomMargin=inch,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Heading1"],
        fontSize=18,
        spaceAfter=12,
    )
    heading_style = ParagraphStyle(
        "SectionHeading",
        parent=styles["Heading2"],
        fontSize=12,
        spaceBefore=12,
        spaceAfter=6,
    )
    story = []

    story.append(Paragraph("Odometer Report", title_style))
    story.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", styles["Normal"]))
    story.append(Spacer(1, 12))

    # Vehicle
    name = vehicle_name or "Unnamed Vehicle"
    story.append(Paragraph("Vehicle", heading_style))
    story.append(Paragraph(name, styles["Normal"]))
    story.append(Spacer(1, 8))

    # Usage stats
    story.append(Paragraph("Usage", heading_style))
    usage_data = [
        ["Total Uptime", _fmt_time(stats.get("total_minutes", 0))],
        ["Armed", _fmt_time(stats.get("armed_minutes", 0))],
        ["Disarmed", _fmt_time(stats.get("disarmed_minutes", 0))],
        ["Dive Time", _fmt_time(stats.get("dive_minutes", 0))],
        ["Battery Swaps", str(stats.get("battery_swaps", 0))],
        ["Startups", str(stats.get("startups", 0))],
        ["Voltage", f"{stats.get('last_voltage', 0):.1f} V"],
        ["Lifetime Energy", f"{stats.get('total_wh_consumed', 0):.1f} Wh"],
    ]
    t = Table(usage_data, colWidths=[2 * inch, 2 * inch])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
    ]))
    story.append(t)
    story.append(Spacer(1, 12))

    # Usage sessions (all missions with max PWM)
    all_sessions = []
    if current_mission and current_mission.get("start_time"):
        cm = {**current_mission, "status": "Active", "hard_use": False}
        all_sessions.append(cm)
    all_sessions.extend([{**m, "status": "Done"} for m in missions])
    if all_sessions:
        story.append(Paragraph("Usage Sessions", heading_style))
        rows = [["Start", "End", "Start V", "End V", "Ah", "Max PWM (us)", "Hard use"]]
        for s in all_sessions:
            start_iso = s.get("start_time")
            end_iso = s.get("end_time")
            if s.get("status") == "Active":
                end_disp = "Now"
            else:
                end_disp = _fmt_iso(end_iso)
            rows.append([
                _fmt_iso(start_iso),
                end_disp,
                f"{s.get('start_voltage', 0):.1f}",
                f"{s.get('end_voltage', 0):.1f}",
                f"{s.get('total_ah', 0):.2f}",
                f"{s.get('max_pwm_deviation', 0):.0f}",
                "Yes" if s.get("hard_use") else "-",
            ])
        t = Table(rows, colWidths=[1.2 * inch, 1.2 * inch, 0.7 * inch, 0.7 * inch, 0.6 * inch, 1.0 * inch, 0.7 * inch])
        t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ]))
        story.append(t)
        story.append(Spacer(1, 8))

    # Hard-use highlights
    hard_use_missions = [m for m in missions if m.get("hard_use")]
    if hard_use_missions:
        story.append(Paragraph("Hard-Use Sessions", heading_style))
        story.append(Paragraph("Sessions with rapid discharge or high duty (>300 us from neutral):", styles["Normal"]))
        for m in hard_use_missions[:10]:
            story.append(Paragraph(
                f"• {_fmt_iso(m.get('start_time'))} – {_fmt_iso(m.get('end_time'))} "
                f"(ΔV: {m.get('start_voltage', 0):.1f}→{m.get('end_voltage', 0):.1f}V, "
                f"max PWM dev: {m.get('max_pwm_deviation', 0):.0f} us)",
                styles["Normal"],
            ))
        story.append(Spacer(1, 8))

    # Thrusters
    if thrusters.get("thruster_count", 0) > 0:
        story.append(Paragraph("Thrusters / Motors", heading_style))
        unit = thrusters.get("layout", {}).get("unit", "thruster")
        rows = [["#", "Run Time", "Avg PWM (us)"]]
        for t in thrusters.get("thrusters", []):
            rows.append([
                str(t.get("id", "")),
                _fmt_time(t.get("run_minutes", 0)),
                str(t.get("avg_pwm_armed", 0) or "-"),
            ])
        t = Table(rows, colWidths=[0.8 * inch, 1.2 * inch, 1.2 * inch])
        t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ]))
        story.append(t)
        story.append(Spacer(1, 8))

    # Accessories
    if accessories:
        story.append(Paragraph("Accessories", heading_style))
        rows = [["Name", "Ch", "Run Time", "Avg PWM (us)"]]
        for a in accessories:
            rows.append([
                a.get("name", ""),
                str(a.get("channel", "")),
                _fmt_time(a.get("run_minutes", 0)),
                str(a.get("avg_pwm_armed", 0) or "-"),
            ])
        t = Table(rows, colWidths=[1.5 * inch, 0.5 * inch, 1 * inch, 1 * inch])
        t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ]))
        story.append(t)
        story.append(Spacer(1, 8))

    # Maintenance
    story.append(Paragraph("Maintenance Log", heading_style))
    if maintenance:
        rows = [["Date", "Event", "Details"]]
        for rec in maintenance[-20:]:
            details = rec.get("details", "") or ""
            details_escaped = html.escape(details).replace("\n", "<br/>")
            rows.append([
                _fmt_iso(rec.get("timestamp", "")),
                rec.get("event_type", ""),
                Paragraph(details_escaped, ParagraphStyle("Details", parent=styles["Normal"], fontSize=9)),
            ])
        t = Table(rows, colWidths=[1.4 * inch, 1.1 * inch, 3.5 * inch])
        t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(t)
    else:
        story.append(Paragraph("No maintenance records.", styles["Normal"]))

    doc.build(story)
    return buffer.getvalue()
