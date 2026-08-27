#!/usr/bin/env python3
"""Render README.md to docs/CloudFinOps-Sentinel-Documentation.pdf.

The PDF is generated from the README rather than maintained beside it, so the
two cannot drift. Run it after any README change:

    python scripts/build_pdf.py

Needs `markdown` and `playwright` (with chromium):
    pip install markdown playwright && playwright install chromium
"""

import asyncio
import base64
import os
import re
import sys

import markdown
from playwright.async_api import async_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "CloudFinOps-Sentinel-Documentation.pdf")
TMP = os.path.join(ROOT, "docs", "_pdf.html")

CSS = """
@page { size: A4; margin: 17mm 15mm 20mm 15mm; }
* { box-sizing: border-box; }
body { font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 9.6pt;
       line-height: 1.55; color: #1b2432; margin: 0; }
h1 { font-size: 22pt; color: #0b3d91; margin: 0 0 4pt; letter-spacing: -0.4pt; }
h2 { font-size: 14pt; color: #0b3d91; margin: 20pt 0 7pt; padding-bottom: 4pt;
     border-bottom: 1.2pt solid #0b3d91; page-break-after: avoid; }
h3 { font-size: 11.2pt; color: #23324a; margin: 14pt 0 5pt; page-break-after: avoid; }
h4 { font-size: 10pt; color: #23324a; margin: 11pt 0 4pt; page-break-after: avoid; }
p { margin: 0 0 7pt; }
a { color: #0b57c0; text-decoration: none; }
code { font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 8.4pt;
       background: #eef2f8; padding: 1pt 3pt; border-radius: 2pt; color: #14304f; }
pre { background: #0f1b30; color: #e6edf8; padding: 8pt 10pt; border-radius: 4pt;
      overflow: hidden; page-break-inside: avoid; margin: 0 0 9pt; }
pre code { background: none; color: inherit; font-size: 8.1pt; padding: 0;
           white-space: pre-wrap; word-break: break-word; }
table { border-collapse: collapse; width: 100%; margin: 0 0 10pt; font-size: 8.6pt;
        page-break-inside: avoid; }
th { background: #0b3d91; color: #fff; text-align: left; padding: 4.5pt 6pt; font-weight: 600; }
td { padding: 4.5pt 6pt; border-bottom: 0.5pt solid #d7dfea; vertical-align: top; }
tr:nth-child(even) td { background: #f5f8fc; }
blockquote { border-left: 2.5pt solid #9fb3cd; margin: 0 0 9pt; padding: 2pt 0 2pt 10pt;
             color: #4a5b73; font-style: italic; }
ul, ol { margin: 0 0 8pt; padding-left: 16pt; }
li { margin-bottom: 2.5pt; }
hr { border: none; border-top: 0.6pt solid #ccd6e4; margin: 14pt 0; }
figure { margin: 10pt 0 13pt; page-break-inside: avoid; text-align: center; }
figure img { max-width: 100%; border: 0.6pt solid #c3cfe0; border-radius: 3pt; }
figcaption { font-size: 7.8pt; color: #6b7a90; margin-top: 3.5pt; font-style: italic; }
h1 + p { font-size: 10.6pt; color: #3d4b60; }
"""

COVER = """
<div style="page-break-after:always; padding-top:52mm; text-align:center;">
  <div style="font-size:31pt;font-weight:700;color:#0b3d91;letter-spacing:-0.8pt;">CloudFinOps Sentinel</div>
  <div style="font-size:13pt;color:#3d4b60;margin-top:7pt;">Autonomous cost optimization and auditing for Google Cloud</div>
  <div style="width:64mm;height:1.6pt;background:#0b3d91;margin:16pt auto;"></div>
  <div style="font-size:10.4pt;color:#4a5b73;line-height:2;">
    Gemini 3.5 Flash-Lite &middot; Gemma &middot; GenAI SDK<br>
    Cloud Run &middot; Firestore &middot; Cloud Scheduler &middot; Secret Manager &middot; Cloud Trace
  </div>
  <div style="margin-top:26pt;font-size:9.4pt;color:#6b7a90;">Technical documentation</div>
  <div style="font-size:9.4pt;color:#6b7a90;">github.com/Mgodoyd/CloudFinOps-Sentinel-Agent</div>
</div>
"""


def build_html() -> str:
    md = open(os.path.join(ROOT, "README.md")).read()
    # The in-page table of contents is navigation for GitHub; a PDF has its own.
    md = re.sub(r"## Contents\n(?:.*\n)*?\n---\n", "", md, count=1)
    html = markdown.markdown(md, extensions=["tables", "fenced_code", "toc", "attr_list"])

    def embed(match):
        alt, src = match.group(1), match.group(2)
        path = os.path.join(ROOT, src)
        if not os.path.exists(path):
            return match.group(0)
        data = base64.b64encode(open(path, "rb").read()).decode()
        return (f'<figure><img alt="{alt}" src="data:image/png;base64,{data}">'
                f'<figcaption>{alt}</figcaption></figure>')

    # Inlined so the PDF is one self-contained file.
    return re.sub(r'<img alt="([^"]*)" src="([^"]+)"\s*/?>', embed, html)


async def render() -> None:
    open(TMP, "w").write(
        f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style>"
        f"</head><body>{COVER}{build_html()}</body></html>"
    )
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(f"file://{TMP}", wait_until="networkidle")
        await page.wait_for_timeout(2500)
        await page.pdf(
            path=OUT, format="A4", print_background=True,
            margin={"top": "17mm", "bottom": "20mm", "left": "15mm", "right": "15mm"},
            display_header_footer=True, header_template="<div></div>",
            footer_template=(
                "<div style='width:100%;font-size:7.5pt;color:#8a97a8;"
                "font-family:Helvetica,Arial,sans-serif;padding:0 15mm;'>"
                "<span style='float:left'>CloudFinOps Sentinel &middot; Technical documentation</span>"
                "<span style='float:right'><span class='pageNumber'></span> / "
                "<span class='totalPages'></span></span></div>"),
        )
        await browser.close()
    os.remove(TMP)
    print(f"wrote {os.path.relpath(OUT, ROOT)}")


if __name__ == "__main__":
    try:
        asyncio.run(render())
    except Exception as exc:
        print(f"could not build the PDF: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
