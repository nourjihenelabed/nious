#!/usr/bin/env python3
"""Build the data-lineage explorer: the interactive page and the printable PDF.

    python scripts/build_lineage.py            # both
    python scripts/build_lineage.py --pdf-only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

# The artifact's light palette, so the two read as the same document.
INK = colors.HexColor("#17201C")
INK2 = colors.HexColor("#5A6660")
INK3 = colors.HexColor("#8A948E")
RULE = colors.HexColor("#D6DCD6")
RULE2 = colors.HexColor("#E7EBE6")
STRUCTURE = colors.HexColor("#14615C")
SIGNAL = colors.HexColor("#B0761A")
STOP = colors.HexColor("#9C4750")
PLATE2 = colors.HexColor("#F7F9F6")

#: Windows ships these, and they map onto the artifact's IBM Plex trio: a serif
#: for prose, a mono for every number and path, a condensed sans for headings.
FONTS = {
    "Body": "georgia.ttf",
    "Body-Bold": "georgiab.ttf",
    "Body-Italic": "georgiai.ttf",
    "Mono": "consola.ttf",
    "Mono-Bold": "consolab.ttf",
    "Head": "ARIALN.TTF",
    "Head-Bold": "ARIALNB.TTF",
}

ROLE_LABEL = {
    "carries": "carries the number",
    "abstains": "abstains here",
    "skipped": "not on this path",
}
ROLE_COLOUR = {"carries": SIGNAL, "abstains": STOP, "skipped": INK3}


#: Logical role -> built-in face, used when the system fonts are not there.
FALLBACK = {
    "Body": "Times-Roman", "Body-Bold": "Times-Bold", "Body-Italic": "Times-Italic",
    "Mono": "Courier", "Mono-Bold": "Courier-Bold",
    "Head": "Helvetica", "Head-Bold": "Helvetica-Bold",
}


def register_fonts() -> tuple[dict[str, str], bool]:
    """Register the system TTFs, or fall back to the built-in faces."""
    fonts_dir = Path("C:/Windows/Fonts")
    try:
        for role, filename in FONTS.items():
            path = fonts_dir / filename
            if not path.exists():
                raise FileNotFoundError(path)
            pdfmetrics.registerFont(TTFont(role, str(path)))
        pdfmetrics.registerFontFamily(
            "Body", normal="Body", bold="Body-Bold", italic="Body-Italic"
        )
        return {role: role for role in FONTS}, True
    except Exception as exc:
        print(f"[fonts] {exc}; falling back to the built-in faces", file=sys.stderr)
        return dict(FALLBACK), False


def styles(F: dict[str, str]) -> dict[str, ParagraphStyle]:
    """Every paragraph style the document uses."""
    return {
        "eyebrow": ParagraphStyle(
            "eyebrow", fontName=F["Mono"], fontSize=7.5, leading=11, textColor=STRUCTURE,
            spaceAfter=5,
        ),
        "title": ParagraphStyle(
            "title", fontName=F["Head-Bold"], fontSize=30, leading=32, textColor=INK,
            spaceAfter=8,
        ),
        "standfirst": ParagraphStyle(
            "standfirst", fontName=F["Body"], fontSize=10.5, leading=15.5, textColor=INK2,
            spaceAfter=8,
        ),
        "meta": ParagraphStyle(
            "meta", fontName=F["Mono"], fontSize=7.5, leading=12, textColor=INK3,
            spaceAfter=2,
        ),
        "part": ParagraphStyle(
            "part", fontName=F["Head-Bold"], fontSize=17, leading=20, textColor=INK,
            spaceBefore=6, spaceAfter=3,
        ),
        "partnote": ParagraphStyle(
            "partnote", fontName=F["Body-Italic"], fontSize=9.5, leading=14, textColor=INK2,
            spaceAfter=12,
        ),
        "stage": ParagraphStyle(
            "stage", fontName=F["Head-Bold"], fontSize=12.5, leading=15, textColor=INK,
            spaceBefore=2, spaceAfter=2,
        ),
        "where": ParagraphStyle(
            "where", fontName=F["Mono"], fontSize=7.8, leading=11.5, textColor=STRUCTURE,
            spaceAfter=3,
        ),
        "tools": ParagraphStyle(
            "tools", fontName=F["Mono"], fontSize=7.2, leading=11, textColor=INK3,
            spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "body", fontName=F["Body"], fontSize=9.6, leading=14.2, textColor=INK,
            alignment=TA_JUSTIFY, spaceAfter=5,
        ),
        "note": ParagraphStyle(
            "note", fontName=F["Body"], fontSize=9.4, leading=14, textColor=INK2,
            alignment=TA_JUSTIFY, leftIndent=8, borderPadding=0, spaceAfter=5,
        ),
        "role": ParagraphStyle(
            "role", fontName=F["Mono-Bold"], fontSize=7.5, leading=11, spaceAfter=4,
        ),
        "cell": ParagraphStyle("cell", fontName=F["Mono"], fontSize=7.4, leading=10.6, textColor=INK2),
        "cellhead": ParagraphStyle(
            "cellhead", fontName=F["Mono-Bold"], fontSize=6.6, leading=10, textColor=INK
        ),
        "th": ParagraphStyle("th", fontName=F["Mono-Bold"], fontSize=6.8, leading=10, textColor=INK3),
        "td": ParagraphStyle("td", fontName=F["Mono"], fontSize=8, leading=11.5, textColor=INK),
        "tdmuted": ParagraphStyle(
            "tdmuted", fontName=F["Mono"], fontSize=8, leading=11.5, textColor=INK3
        ),
        "tdnum": ParagraphStyle(
            "tdnum", fontName=F["Mono"], fontSize=8, leading=11.5, textColor=INK,
            alignment=TA_RIGHT,
        ),
        "tdna": ParagraphStyle(
            "tdna", fontName=F["Mono"], fontSize=8, leading=11.5, textColor=STOP,
            alignment=TA_RIGHT,
        ),
        "tdtotal": ParagraphStyle(
            "tdtotal", fontName=F["Mono-Bold"], fontSize=8, leading=11.5, textColor=SIGNAL,
            alignment=TA_RIGHT,
        ),
        # The closing row's label reads left with the rest of the column; only its number is
        # right-aligned.
        "tdtotalname": ParagraphStyle(
            "tdtotalname", fontName=F["Mono-Bold"], fontSize=8, leading=11.5, textColor=INK,
        ),
        "foot": ParagraphStyle("foot", fontName=F["Mono"], fontSize=7, leading=10, textColor=INK3),
    }


def esc(text: str) -> str:
    """XML-escape for Paragraph, and normalise the glyphs Georgia lacks."""
    out = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return out.replace("\u2212", "-")


def rich(text: str, mono: str = "Mono") -> str:
    """Same, but render the emphasis and code spans the source notes use."""
    out = esc(text)
    out = out.replace("&lt;em&gt;", "<i>").replace("&lt;/em&gt;", "</i>")
    return re.sub(r"`([^`]+)`", rf'<font name="{mono}" size="8.6"></font>', out)


def mono_block(text: str, style) -> Paragraph:
    """Monospaced text with its newlines preserved."""
    return Paragraph(esc(text).replace("\n", "<br/>"), style)


def calc_table(stage: dict, S: dict, width: float) -> Table:
    """One arithmetic table: step, how it was derived, the value."""
    head = stage.get("calcHead") or ["step", "how", "value"]
    rows = [[Paragraph(esc(h), S["th"]) for h in head]]
    total_row = len(stage["calc"]) - 1 if stage.get("total") else -1

    for i, (a, b, c) in enumerate(stage["calc"]):
        is_na = str(c).strip().lower() == "n/a"
        num_style = S["tdtotal"] if i == total_row else (S["tdna"] if is_na else S["tdnum"])
        rows.append(
            [
                Paragraph(esc(a), S["tdtotalname"] if i == total_row else S["td"]),
                Paragraph(esc(b), S["tdmuted"]),
                Paragraph(esc(c), num_style),
            ]
        )

    table = Table(rows, colWidths=[width * 0.34, width * 0.42, width * 0.24], hAlign="LEFT")
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, RULE),
        ("LINEBELOW", (0, 1), (-1, -2), 0.3, RULE2),
    ]
    if total_row >= 0:
        style.append(("LINEABOVE", (0, total_row + 1), (-1, total_row + 1), 0.9, INK))
    table.setStyle(TableStyle(style))
    return table


def io_table(stage: dict, S: dict, width: float) -> Table:
    """The in/out pair for one stage."""
    inp, out = stage["io"]
    rows = [
        [Paragraph("IN", S["cellhead"]), Paragraph("OUT", S["cellhead"])],
        [mono_block(inp, S["cell"]), mono_block(out, S["cell"])],
    ]
    table = Table(rows, colWidths=[width * 0.49, width * 0.49], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, -1), PLATE2),
                ("BOX", (0, 0), (0, -1), 0.3, RULE2),
                ("BOX", (1, 0), (1, -1), 0.3, RULE2),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, 0), 6),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 6),
                ("TOPPADDING", (0, 1), (-1, 1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 1),
            ]
        )
    )
    return table


def build_pdf(data: dict, out_path: Path, F: dict[str, str]) -> Path:
    """Typeset the whole lineage as a printable document."""
    S = styles(F)
    width = A4[0] - 40 * mm

    doc = BaseDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title="Waterfall Lineage",
        author="Nourjihen Elabed",
        subject="AXIOM Pricing Module - how every published number is derived",
    )

    def decorate(canvas, document):
        """Running foot: what this is, and where you are in it."""
        canvas.saveState()
        canvas.setFont(F["Mono"], 7)
        canvas.setFillColor(INK3)
        canvas.drawString(
            20 * mm, 11 * mm, "AXIOM Pricing Module - data lineage - engine v0.4.0"
        )
        canvas.drawRightString(A4[0] - 20 * mm, 11 * mm, str(document.page))
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.4)
        canvas.line(20 * mm, 14 * mm, A4[0] - 20 * mm, 14 * mm)
        canvas.restoreState()

    frame = Frame(
        doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body",
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
    )
    doc.addPageTemplates([PageTemplate(id="all", frames=[frame], onPage=decorate)])

    story = []
    stages = data["stages"]
    traces = data["traces"]

    # -- masthead ----------------------------------------------------------
    story += [
        Paragraph("AXIOM PRICING MODULE &#183; TRACK A &#183; DATA LINEAGE", S["eyebrow"]),
        Paragraph("How do I get that number?", S["title"]),
        Paragraph(
            "Pick a number and follow it back to the bytes it came from. Every stage "
            "names the module and function that runs it, the library doing the work, "
            "and what the number actually looks like at that point.",
            S["standfirst"],
        ),
        Paragraph(
            "engine v0.4.0 &#183; contract v0.2.0-draft &#183; 178 tests &#183; "
            "profiles b2b_industrial, dairy_al_kharj",
            S["meta"],
        ),
        Spacer(1, 4),
        HRFlowable(width="100%", thickness=1.4, color=INK, spaceAfter=14),
        Paragraph(
            "The interactive version of this document shows one trace at a time; this "
            "one shows all three, with the arithmetic already open. Part one describes "
            "the pipeline once. Part two follows three different numbers through it - "
            "including one that deliberately refuses to answer.",
            S["body"],
        ),
        Spacer(1, 10),
    ]

    # -- part 1: the pipeline ---------------------------------------------
    story += [
        Paragraph("Part one &#183; The pipeline", S["part"]),
        Paragraph("Ten stages, in the order they run.", S["partnote"]),
    ]
    for stage in stages:
        story.append(
            KeepTogether(
                [
                    Paragraph(f'{stage["n"]}. {esc(stage["name"])}', S["stage"]),
                    Paragraph(esc(stage["where"]), S["where"]),
                    Paragraph(" &#183; ".join(esc(t) for t in stage["tools"]), S["tools"]),
                    Paragraph(rich(stage["what"], F["Mono"]), S["body"]),
                    Spacer(1, 5),
                ]
            )
        )

    # -- part 2: the traces ------------------------------------------------
    for trace in traces.values():
        story.append(PageBreak())
        story += [
            Paragraph(f"Trace &#183; {esc(trace['q'])}", S["part"]),
            Paragraph(
                f"Answer: {esc(trace['a'])}  &#183;  {esc(trace['src'])}", S["partnote"]
            ),
        ]

        for stage in stages:
            st = trace["stages"][stage["id"]]
            role = st["role"]
            block = [
                Paragraph(f'{stage["n"]}. {esc(stage["name"])}', S["stage"]),
                Paragraph(
                    f'<font color="#{ROLE_COLOUR[role].hexval()[2:]}">'
                    f"{ROLE_LABEL[role].upper()}</font>"
                    f'  <font color="#{INK3.hexval()[2:]}">|</font>  {esc(st["val"])}',
                    S["role"],
                ),
            ]
            if role != "skipped":
                block += [io_table(st, S, width), Spacer(1, 6)]
            block.append(Paragraph(rich(st["note"], F["Mono"]), S["note"]))
            if st.get("calc"):
                block += [Spacer(1, 3), calc_table(st, S, width)]
            block.append(Spacer(1, 11))
            story.append(KeepTogether(block))

    # -- colophon ----------------------------------------------------------
    story += [
        Spacer(1, 6),
        HRFlowable(width="100%", thickness=0.6, color=RULE, spaceAfter=8),
        Paragraph("Reproduce any of this:  dvc repro", S["foot"]),
        Paragraph("Check your data first:  python scripts/check_data.py", S["foot"]),
        Paragraph(
            "Provenance: every result carries a sha256 over the source bytes, so a "
            "number can always be traced to the exact snapshot that produced it.",
            S["foot"],
        ),
        Paragraph(
            "Regenerate this document:  python scripts/build_lineage.py", S["foot"]
        ),
    ]

    doc.build(story)
    return out_path


def build_html(data: dict, out_path: Path) -> Path:
    """Inject the same content into the interactive template."""
    template = (DOCS / "lineage_template.html").read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    out_path.write_text(template.replace("__LINEAGE_DATA__", payload), encoding="utf-8")
    return out_path


def main(argv: list[str] | None = None) -> int:
    """Build whichever outputs were asked for."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf-only", action="store_true")
    parser.add_argument("--html-only", action="store_true")
    args = parser.parse_args(argv)

    data = json.loads((DOCS / "lineage.json").read_text(encoding="utf-8"))
    fonts, real_fonts = register_fonts()

    if not args.pdf_only:
        path = build_html(data, DOCS / "lineage.html")
        print(f"html : {path}  ({path.stat().st_size:,} bytes)")
    if not args.html_only:
        path = build_pdf(data, DOCS / "lineage.pdf", fonts)
        note = "" if real_fonts else "  [built-in fonts]"
        print(f"pdf  : {path}  ({path.stat().st_size:,} bytes){note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
