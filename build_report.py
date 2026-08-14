"""
build_report.py
----------------
Turns one run_daily.py result dict into a formula-driven Excel workbook.

House style: Arial · blue text = value produced by the analysis pipeline
(price, verdict, confidence — the report's inputs) · black text = an Excel
formula computed from cells in this workbook · green text = a formula that
links to another sheet. "Fired" is a real formula (Verdict + Confidence vs.
the threshold cell), not a hardcoded flag, so it recalculates if you change
the threshold on the Summary tab.

Can be run standalone against a saved result for testing:
    python build_report.py path/to/result.json out.xlsx
"""
import sys
import json

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.formatting.rule import CellIsRule, FormulaRule

FONT = "Arial"
HEADER_FILL = PatternFill("solid", fgColor="1A4D5C")
HEADER_FONT = Font(name=FONT, size=11, bold=True, color="FFFFFF")
TITLE_FONT = Font(name=FONT, size=16, bold=True, color="1A4D5C")
SUB_FONT = Font(name=FONT, size=10, italic=True, color="6B675F")
INPUT_FONT = Font(name=FONT, size=10, color="0000FF")     # blue = pipeline output (input to this sheet)
FORMULA_FONT = Font(name=FONT, size=10, color="000000")   # black = in-sheet formula
LINK_FONT = Font(name=FONT, size=10, color="008000")      # green = cross-sheet link
THIN = Side(style="thin", color="D8D3C7")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

VERDICT_HEADERS = ["Symbol", "Name", "Cap Segment", "Verdict", "Confidence",
                    "Winner", "Price (₹)", "Day Change %", "Key Catalyst",
                    "Rationale", "Engine", "Fired"]


def build(result, output_path="daily_digest.xlsx"):
    verdicts = result["verdicts"]
    last_row = len(verdicts) + 1  # header is row 1

    wb = Workbook()

    # -----------------------------------------------------------------
    # Sheet 1: Summary
    # -----------------------------------------------------------------
    ws = wb.active
    ws.title = "Summary"
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 24

    ws["A1"] = "Bourse — Daily Analysis Digest"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = f"Generated {result['timestamp']}  ·  engine: {result['engine']}  ·  mode: {result['mode']}"
    ws["A2"].font = SUB_FONT

    ws["A3"] = ("Live NSE data pulled via yfinance and run through the same Scout -> Technician -> "
                "Fundamentalist -> Newsdesk -> Bull/Bear -> Judge pipeline as the local dashboard's "
                "Live mode — run here on GitHub Actions, which (unlike a locked-down sandbox) has "
                "normal internet access.")
    ws["A3"].font = Font(name=FONT, size=9, italic=True, color="6B675F")
    ws["A3"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells("A3:D3")
    ws.row_dimensions[3].height = 44

    ws["A5"] = "Confidence threshold (BUY fires at ≥)"
    ws["A5"].font = Font(name=FONT, size=10, bold=True)
    thr_cell = ws.cell(row=5, column=2, value=result["confidence_threshold"])
    thr_cell.font = INPUT_FONT
    thr_cell.fill = PatternFill("solid", fgColor="FFFFCC")
    THRESHOLD_REF = "Summary!$B$5"

    rows = [
        ("Universe scanned", result["universe"]),
        ("Shortlisted / in debate", result["in_debate"]),
        ("BUY signals fired", f"=COUNTIF('All Verdicts'!L2:L{last_row},\"YES\")"),
        ("Avg confidence (all)", f"=IFERROR(ROUND(AVERAGE('All Verdicts'!E2:E{last_row}),2),\"—\")"),
        ("Top BUY confidence", f"=IFERROR(MAX('All Verdicts'!M2:M{last_row}),\"—\")"),
        ("Top pick", f"=IFERROR(INDEX('All Verdicts'!A2:A{last_row},"
                      f"MATCH(MAX('All Verdicts'!M2:M{last_row}),'All Verdicts'!M2:M{last_row},0)),\"—\")"),
    ]
    row = 7
    for label, val in rows:
        ws.cell(row=row, column=1, value=label).font = Font(name=FONT, size=10, bold=True)
        c = ws.cell(row=row, column=2, value=val)
        c.font = INPUT_FONT if isinstance(val, (int, float)) else LINK_FONT
        row += 1

    ws["A14"] = ("Full breakdown on the “All Verdicts” tab. This is the same analysis engine as "
                 "the local dashboard — not a lighter scrape — because GitHub Actions can reach "
                 "yfinance directly.")
    ws["A14"].font = Font(name=FONT, size=9, italic=True, color="6B675F")
    ws["A14"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells("A14:D14")
    ws.row_dimensions[14].height = 32

    ws["A16"] = "Legend:"
    ws["A16"].font = Font(name=FONT, size=9, bold=True)
    ws["A17"] = "Blue = pipeline output (this report's input)"
    ws["A17"].font = INPUT_FONT
    ws["A18"] = "Black / green = formula (recalculates; green links to another sheet)"
    ws["A18"].font = LINK_FONT
    ws["A19"] = "Yellow fill = edit this to change the BUY threshold used by the Fired column"
    ws["A19"].font = Font(name=FONT, size=9, italic=True, color="6B675F")

    # -----------------------------------------------------------------
    # Sheet 2: All Verdicts
    # -----------------------------------------------------------------
    ws2 = wb.create_sheet("All Verdicts")
    for col, h in enumerate(VERDICT_HEADERS, 1):
        c = ws2.cell(row=1, column=col, value=h)
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = BORDER
    ws2.row_dimensions[1].height = 30
    ws2.freeze_panes = "A2"

    widths = [12, 20, 12, 10, 11, 9, 12, 12, 26, 34, 13, 8]
    for i, w in enumerate(widths, 1):
        ws2.column_dimensions[get_column_letter(i)].width = w

    for i, v in enumerate(verdicts, start=2):
        ws2.cell(row=i, column=1, value=v["symbol"]).font = Font(name=FONT, size=10, bold=True)
        ws2.cell(row=i, column=2, value=v["name"]).font = INPUT_FONT
        ws2.cell(row=i, column=3, value=v["cap"].capitalize() if v["cap"] else v["cap"]).font = INPUT_FONT
        ws2.cell(row=i, column=4, value=v["verdict"]).font = INPUT_FONT

        cc = ws2.cell(row=i, column=5, value=v["confidence"])
        cc.font = INPUT_FONT

        ws2.cell(row=i, column=6, value=v["winner"]).font = INPUT_FONT

        pc = ws2.cell(row=i, column=7, value=v["price"])
        pc.font = INPUT_FONT
        pc.number_format = "#,##0.00"

        dc = ws2.cell(row=i, column=8, value=v["day_change_pct"])
        dc.font = INPUT_FONT
        dc.number_format = "0.00"

        ws2.cell(row=i, column=9, value=v["key_catalyst"]).font = INPUT_FONT
        rc = ws2.cell(row=i, column=10, value=v["rationale"])
        rc.font = INPUT_FONT
        rc.alignment = Alignment(wrap_text=True, vertical="top")

        ws2.cell(row=i, column=11, value=v["engine"]).font = Font(name=FONT, size=9, color="6B675F")

        # Fired: real formula against the Summary threshold cell, not the
        # Python-computed boolean, so editing the threshold recalculates it.
        fired_formula = f'=IF(AND(D{i}="BUY",E{i}>={THRESHOLD_REF}),"YES","-")'
        fc = ws2.cell(row=i, column=12, value=fired_formula)
        fc.font = LINK_FONT
        fc.alignment = Alignment(horizontal="center")

        # helper column M (BUY-only confidence) drives the Summary "Top pick" lookup
        mc = ws2.cell(row=i, column=13, value=f'=IF(D{i}="BUY",E{i},"")')
        mc.font = FORMULA_FONT

        for col in range(1, 14):
            ws2.cell(row=i, column=col).border = BORDER

    ws2.cell(row=1, column=13, value="BUY Confidence (helper)").font = HEADER_FONT
    ws2.cell(row=1, column=13).fill = HEADER_FILL
    ws2.column_dimensions["M"].width = 20

    ws2.auto_filter.ref = f"A1:M{last_row}"

    # conditional formatting
    green = PatternFill("solid", fgColor="E8F4EE")
    red = PatternFill("solid", fgColor="FBE9E7")
    gray = PatternFill("solid", fgColor="EFECE6")
    ws2.conditional_formatting.add(f"D2:D{last_row}",
        CellIsRule(operator="equal", formula=['"BUY"'], fill=green))
    ws2.conditional_formatting.add(f"D2:D{last_row}",
        CellIsRule(operator="equal", formula=['"AVOID"'], fill=red))
    ws2.conditional_formatting.add(f"D2:D{last_row}",
        CellIsRule(operator="equal", formula=['"WATCH"'], fill=gray))
    ws2.conditional_formatting.add(f"H2:H{last_row}", FormulaRule(formula=["H2>0"], fill=green))
    ws2.conditional_formatting.add(f"H2:H{last_row}", FormulaRule(formula=["H2<0"], fill=red))
    ws2.conditional_formatting.add(f"L2:L{last_row}",
        CellIsRule(operator="equal", formula=['"YES"'], fill=green))

    wb.save(output_path)
    return output_path


if __name__ == "__main__":
    in_path = sys.argv[1] if len(sys.argv) > 1 else "result.json"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "daily_digest.xlsx"
    with open(in_path) as f:
        result = json.load(f)
    build(result, out_path)
    print(f"saved {out_path}")
