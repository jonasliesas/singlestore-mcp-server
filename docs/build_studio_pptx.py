"""Build docs/studio-slides.pptx (the SAS Innovate deck) on the SAS PowerPoint template.

Runs PowerPoint through COM (pywin32), so it needs Windows with PowerPoint installed.
Same content as docs/studio-slides.html; this script is the source for the .pptx.

    python docs/build_studio_pptx.py [--png]

--png also exports every slide as PNG to docs/studio-slides-png/ for a visual check.
"""
from __future__ import annotations

import os
import sys

import win32com.client as com

TEMPLATE = r"C:\Users\norjni\OneDrive - SAS\template.pptx"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "studio-slides.pptx")
PNG_DIR = os.path.join(HERE, "studio-slides-png")

# SAS template palette (theme colours), as RGB tuples
NAVY = (0x03, 0x29, 0x54)
BLUE = (0x07, 0x66, 0xD1)
LIGHT = (0x43, 0x98, 0xF9)
PALE = (0xC4, 0xDE, 0xFD)
SLATE = (0x7E, 0x88, 0x9A)
GREY = (0xBA, 0xC0, 0xC9)
WHITE = (0xFF, 0xFF, 0xFF)
BLACK = (0x00, 0x00, 0x00)
SOFT = (0xEE, 0xF4, 0xFD)  # note bars: a lighter tint of PALE

KIND = {"acc": BLUE, "s2": NAVY, "viya": LIGHT, "": GREY}

BOLD, LIGHTF, MONO = "Anova Bold", "Anova Light", "Consolas"

# geometry of the template's content area (points; slide is 1440 x 810)
L, T, W, B = 99, 189, 1242, 730
GAP = 16

ppLayoutTitleOnly = 11
msoShapeRectangle, msoShapeRoundedRectangle, msoShapeOval = 1, 5, 9
msoArrowheadTriangle = 2
msoAnchorTop = 1
ppAlignLeft, ppAlignCenter = 1, 2
ppBulletUnnumbered = 1


def rgb(c):
    r, g, b = c
    return r + g * 256 + b * 65536


class Deck:
    def __init__(self):
        self.app = com.Dispatch("PowerPoint.Application")
        self.pres = self.app.Presentations.Open(TEMPLATE, False, False, False)
        while self.pres.Slides.Count:
            self.pres.Slides(1).Delete()
        self.layouts = {l.Name: l for l in self.pres.SlideMaster.CustomLayouts}

    def layout(self, name):
        return self.layouts["SAS - " + name]

    # ---- slides -------------------------------------------------------
    def slide(self, layout, title=None, notes=None, title_size=40):
        s = self.pres.Slides.AddSlide(self.pres.Slides.Count + 1, self.layout(layout))
        if title is not None:
            tr = s.Shapes.Title.TextFrame.TextRange
            tr.Text = title
            tr.Font.Size = title_size
            if layout not in ("Title", "Closing"):
                tr.Font.Color.RGB = rgb(NAVY)
        if notes:
            s.NotesPage.Shapes.Placeholders(2).TextFrame.TextRange.Text = notes
        return s

    # ---- primitives ---------------------------------------------------
    def box(self, s, x, y, w, h, fill=WHITE, line=GREY, weight=0.75, rounded=True, radius=0.04):
        sh = s.Shapes.AddShape(msoShapeRoundedRectangle if rounded else msoShapeRectangle, x, y, w, h)
        if rounded:
            sh.Adjustments.SetItem(1, radius)
        if fill is None:
            sh.Fill.Visible = False
        else:
            sh.Fill.Solid()
            sh.Fill.ForeColor.RGB = rgb(fill)
        if line is None:
            sh.Line.Visible = False
        else:
            sh.Line.Visible = True
            sh.Line.ForeColor.RGB = rgb(line)
            sh.Line.Weight = weight
        sh.Shadow.Visible = False
        return sh

    def text(self, s, x, y, w, h, runs, align=ppAlignLeft, margin=6, anchor=msoAnchorTop):
        """runs: list of (text, font, size, color, opts) per paragraph; opts: dict(bullet=True, space_before=pt)."""
        tb = s.Shapes.AddTextbox(1, x, y, w, h)
        tf = tb.TextFrame
        tf.WordWrap = True
        tf.AutoSize = 0
        tf.MarginLeft = tf.MarginRight = margin
        tf.MarginTop = tf.MarginBottom = margin
        tf.VerticalAnchor = anchor
        tr = tf.TextRange
        tr.Text = "\r".join(r[0] for r in runs)
        for i, (txt, font, size, color, *rest) in enumerate(runs, start=1):
            opts = rest[0] if rest else {}
            p = tr.Paragraphs(i)
            p.Font.Name = font
            p.Font.Size = size
            p.Font.Color.RGB = rgb(color)
            p.Font.Bold = False
            p.ParagraphFormat.Alignment = align
            p.ParagraphFormat.SpaceBefore = opts.get("space_before", 2)
            p.ParagraphFormat.SpaceAfter = 0
            p.ParagraphFormat.SpaceWithin = 1.05
            if opts.get("bullet"):
                p.ParagraphFormat.Bullet.Visible = True
                p.ParagraphFormat.Bullet.Type = ppBulletUnnumbered
                p.ParagraphFormat.Bullet.Character = 8226
                p.ParagraphFormat.Bullet.Font.Color.RGB = rgb(BLUE)
                p.ParagraphFormat.Bullet.RelativeSize = 0.9
            else:
                p.ParagraphFormat.Bullet.Visible = False
        # hanging indent for bullets: bullets sit on ruler level 2, everything else on level 1
        has_bullets = any(r[4].get("bullet") for r in runs if len(r) > 4)
        tf.Ruler.Levels(1).FirstMargin = 0
        tf.Ruler.Levels(1).LeftMargin = 14 if has_bullets else 0
        for i, (txt, font, size, color, *rest) in enumerate(runs, start=1):
            para = tr.Paragraphs(i)
            para.IndentLevel = 1
            para.Font.Size = size
        return tb

    def arrow(self, s, x1, y1, x2, y2, color=SLATE, weight=1.5, dashed=False):
        ln = s.Shapes.AddLine(x1, y1, x2, y2)
        ln.Line.ForeColor.RGB = rgb(color)
        ln.Line.Weight = weight
        ln.Line.EndArrowheadStyle = msoArrowheadTriangle
        if dashed:
            ln.Line.DashStyle = 4
        return ln

    # ---- composites ---------------------------------------------------
    def card(self, s, x, y, w, h, kind="", chip=None, head=None, body=(), bullets=(), num=None, out=None,
             body_size=13, head_size=16):
        body_size += 3
        head_size += 3
        self.box(s, x, y, w, h)
        bar = s.Shapes.AddLine(x + 6, y + 1, x + w - 6, y + 1)
        bar.Line.ForeColor.RGB = rgb(KIND[kind])
        bar.Line.Weight = 3
        runs = []
        if num is not None:
            runs.append((str(num), BOLD, 28, KIND[kind] if kind else BLUE, {"space_before": 0}))
        if chip:
            runs.append((chip.upper(), BOLD, 11, KIND[kind] if kind else SLATE, {"space_before": 0}))
        if head:
            runs.append((head, BOLD, head_size, NAVY, {"space_before": 2}))
        for p in body:
            runs.append((p, LIGHTF, body_size, BLACK, {"space_before": 4}))
        if out and not bullets:
            runs.append((out, LIGHTF, body_size - 1, SLATE, {"space_before": 8}))
        self.text(s, x + 6, y + 6, w - 12, h - 12, runs, margin=8)
        if bullets:
            # estimate the height of the heading block, then start the bullets under it
            used = 14
            for txt, font, size, color, *rest in runs:
                chars_per_line = max(1, int((w - 28) / (size * 0.5)))
                lines = max(1, -(-len(txt) // chars_per_line))
                used += lines * size * 1.25 + rest[0].get("space_before", 2) if rest else lines * size * 1.25
            bruns = [(b, LIGHTF, body_size, BLACK, {"bullet": True, "space_before": 4}) for b in bullets]
            if out:
                bruns.append((out, LIGHTF, body_size - 1, SLATE, {"space_before": 8}))
            self.text(s, x + 6, y + 6 + used, w - 12, h - 12 - used, bruns, margin=8)

    def note(self, s, y, h, lead, rest):
        self.box(s, L, y, W, h, fill=SOFT, line=None)
        tb = self.text(s, L + 8, y, W - 16, h, [(lead + " " + rest, LIGHTF, 17, NAVY)], anchor=3, margin=10)
        tr = tb.TextFrame.TextRange
        tr.Characters(1, len(lead)).Font.Name = BOLD

    def row(self, s, y, h, cards, **kw):
        n = len(cards)
        w = (W - GAP * (n - 1)) / n
        for i, c in enumerate(cards):
            self.card(s, L + i * (w + GAP), y, w, h, **{**kw, **c})

    def save(self, png=False):
        if os.path.exists(OUT):
            os.remove(OUT)
        self.pres.SaveAs(OUT)
        if png:
            os.makedirs(PNG_DIR, exist_ok=True)
            for f in os.listdir(PNG_DIR):
                os.remove(os.path.join(PNG_DIR, f))
            for i in range(1, self.pres.Slides.Count + 1):
                self.pres.Slides(i).Export(os.path.join(PNG_DIR, f"slide{i:02d}.png"), "PNG", 1280, 720)
        n = self.pres.Slides.Count
        self.pres.Close()
        self.app.Quit()
        return n


def build(png=False):
    d = Deck()

    # 1 Title ---------------------------------------------------------------
    s = d.slide("Title", "SAS SpeedyStore Studio", title_size=66,
                notes="Open with the one sentence: everything you need to work with SAS SpeedyStore is in one window, "
                      "and the assistant sits inside it instead of in a separate chat.")
    s.Shapes.Placeholders(2).TextFrame.TextRange.Text = "Talk to your data platform"
    s.Shapes.Placeholders(3).TextFrame.TextRange.Text = "Jonas Lie-Nielsen  ·  SAS Innovate 2027"

    # 2 Five windows ----------------------------------------------------------
    s = d.slide("Title Only", "Five windows for one job",
                notes="Ask the room how many windows they have open for a normal day with SpeedyStore and Viya. Five is a conservative count.")
    d.row(s, T, 200, [
        dict(kind="s2", chip="Window 1", head="SQL client", body=["Write and run SQL against SingleStore. Knows the syntax, not your schema."]),
        dict(kind="s2", chip="Window 2", head="Notebook", body=["Python for the analysis, a copy of the data pulled across for each try."]),
        dict(kind="s2", chip="Window 3", head="Pipeline and cluster consoles", body=["Loads from Kafka and S3, node health, slow queries. Each in its own tool."]),
        dict(kind="viya", chip="Window 4", head="SAS Studio", body=["The SAS side of the same data: libraries, caslibs, PROC SQL, FedSQL."]),
        dict(kind="acc", chip="Window 5", head="The AI chat", body=["Helpful, but blind: it never sees your tables, and you paste its answer somewhere else."]),
    ], body_size=14, head_size=17)
    d.note(s, T + 220, 70, "The cost.", "Context is lost at every switch. The assistant guesses column names, the notebook copies data "
           "that should stay in the database, and nobody sees the whole picture.")

    # 3 The idea (diagram) ------------------------------------------------------
    s = d.slide("Title Only", "One workspace, with the assistant inside it",
                notes="Three things to point at: the rail on the left, the assistant panel inside the window, and the two systems "
                      "on the right. MCP is how one assistant reaches both, with tools that are read-only by default.")
    k = W / 1136.0
    X = lambda x: L + x * k
    Y = lambda y: T + 10 + y * k
    S = lambda v: v * k
    # person
    d.box(s, X(64), Y(164), S(52), S(52), fill=SOFT, line=GREY).AutoShapeType = msoShapeOval
    d.box(s, X(50), Y(222), S(80), S(40), fill=SOFT, line=GREY, radius=0.5)
    d.text(s, X(20), Y(280), S(140), S(50), [("You", BOLD, 16, NAVY), ("analyst, engineer, DBA", LIGHTF, 12, SLATE)], align=ppAlignCenter)
    # workspace
    d.box(s, X(200), Y(40), S(470), S(360), fill=WHITE, line=BLUE, weight=2, radius=0.05)
    d.text(s, X(212), Y(48), S(440), S(56), [("SAS SpeedyStore Studio", BOLD, 18, NAVY),
                                              ("one window, a rail of views, state kept between them", LIGHTF, 12, SLATE)])
    d.box(s, X(224), Y(124), S(112), S(152), fill=SOFT, line=GREY)
    d.text(s, X(224), Y(128), S(112), S(150),
           [(v, BOLD, 13, NAVY, {"space_before": 5}) for v in ("SQL", "Notebook", "Schema", "Pipelines", "Cluster")]
           + [("+ 4 more views", LIGHTF, 10, SLATE, {"space_before": 5})], align=ppAlignCenter)
    d.box(s, X(352), Y(124), S(296), S(152), fill=WHITE, line=GREY)
    d.text(s, X(358), Y(130), S(286), S(80),
           [("SELECT region, sum(amount)", MONO, 12, NAVY), ("FROM SASDP.Sales", MONO, 12, NAVY), ("GROUP BY region;", MONO, 12, NAVY)])
    d.text(s, X(358), Y(222), S(286), S(50),
           [("Run as: SingleStore SQL, PROC SQL, FedSQL or FedSQL in CAS. Grid, chart, log.", LIGHTF, 11, SLATE)])
    d.box(s, X(224), Y(290), S(424), S(86), fill=PALE, line=BLUE)
    d.text(s, X(232), Y(296), S(410), S(80),
           [("Claude, in the panel", BOLD, 14, NAVY),
            ("reads the schema, runs read-only queries, and writes SQL, Python or SAS code you insert with one click", LIGHTF, 12, NAVY)])
    d.arrow(s, X(132), Y(190), X(198), Y(190))
    # MCP
    d.arrow(s, X(672), Y(150), X(858), Y(150))
    d.arrow(s, X(672), Y(300), X(858), Y(300))
    d.text(s, X(672), Y(118), S(186), S(28), [("SQL, pipelines, monitoring", LIGHTF, 11, SLATE)], align=ppAlignCenter, margin=0)
    d.text(s, X(672), Y(268), S(186), S(28), [("REST: Compute, CAS, files", LIGHTF, 11, SLATE)], align=ppAlignCenter, margin=0)
    d.text(s, X(660), Y(200), S(210), S(50), [("Model Context Protocol", BOLD, 13, BLUE), ("tools an assistant can use", LIGHTF, 11, SLATE)],
           align=ppAlignCenter, margin=0)
    # systems
    d.box(s, X(860), Y(96), S(252), S(110), fill=WHITE, line=NAVY, weight=2, radius=0.08)
    d.text(s, X(866), Y(104), S(240), S(100), [("SAS SpeedyStore", BOLD, 17, NAVY), ("SingleStore, self-managed or Helios", LIGHTF, 12, SLATE),
                                                ("databases, pipelines, nodes", LIGHTF, 12, SLATE)], align=ppAlignCenter)
    d.box(s, X(860), Y(246), S(252), S(110), fill=WHITE, line=LIGHT, weight=2, radius=0.08)
    d.text(s, X(866), Y(254), S(240), S(100), [("SAS Viya", BOLD, 17, NAVY), ("Compute and CAS sessions", LIGHTF, 12, SLATE),
                                                ("SAS Content, libraries, caslibs", LIGHTF, 12, SLATE)], align=ppAlignCenter)
    d.arrow(s, X(986), Y(244), X(986), Y(208), color=LIGHT, dashed=True)
    d.text(s, X(996), Y(212), S(120), S(30), [("SAS/ACCESS, CAS pushdown", LIGHTF, 10, SLATE)], margin=0)
    d.text(s, L, B - 40, W, 36, [("The Studio is the person's workbench. The assistant is a helper inside it, with read-only tools; "
                                   "you review and run what it suggests.", LIGHTF, 13, SLATE)], margin=0)

    # 4 SQL editor -------------------------------------------------------------
    s = d.slide("Title Only", "A SQL editor that knows your schema and your SAS",
                notes="The pushdown tab is the demo moment for SAS people: they see which part of their PROC SQL SingleStore executed.")
    d.row(s, T, 270, [
        dict(kind="s2", chip="SingleStore", head="SQL with the schema at hand", bullets=[
            "Autocomplete for SingleStore SQL, built-in functions with signatures, your databases, tables, columns and procedures",
            "Ctrl+Enter runs the statement under the cursor; Run all runs a whole script and gives each statement its own result tab",
            "Reads run at once; statements that change data or schema ask first",
            "Results grid, sorting and CSV export, a chart from the result, run history, tabs with their own database and file"]),
        dict(kind="viya", chip="SAS Viya", head="The same SQL, through SAS", bullets=[
            "Run as picks the language: SingleStore SQL, PROC SQL, FedSQL or FedSQL in CAS, with the matching tree of databases, libraries or caslibs",
            "Result, the SAS log and a Pushdown tab that shows what SAS handed to SingleStore",
            "Open and save .sql and .sas files on this computer, in SAS Content or on the SAS server, with a version check so nobody's work is overwritten",
            "Long runs go to the background with a Stop button"]),
    ], body_size=14, head_size=18)
    d.note(s, T + 290, 70, "Try it both ways.", "The same query native in SingleStore and through SAS side by side, with the pushdown visible. "
           "That is the SpeedyStore conversation in one tab.")

    # 5 Notebook ------------------------------------------------------------------
    s = d.slide("Title Only", "A notebook where the data stays in the database",
                notes="The point for SAS Viya: the data never leaves the database for a local copy. SAS reads SingleStore itself through the S2 library, and CAS pushes queries down.")
    d.row(s, T, 230, [
        dict(kind="s2", chip="SQL cells", body=["Run against SingleStore; the result becomes a pandas DataFrame in the next cell. %sql and %%sql magics for the Python crowd."]),
        dict(kind="acc", chip="Python cells", body=["A Jupyter kernel in its own environment with a Packages panel. Charts inline. The notebook file is a normal .ipynb, compatible with SingleStore Notebooks."]),
        dict(kind="viya", chip="SAS cells", body=["Run in a SAS Compute session on Viya. Each session gets the library S2 on the notebook's database, so a DATA step reads and writes SingleStore directly through SAS/ACCESS."]),
    ], body_size=15)
    d.row(s, T + 250, 150, [
        dict(head="Several notebooks as tabs", body=["Each with its own kernel, database and file. Save to this computer, SAS Content or the SAS server."]),
        dict(head="Claude in the side panel", body=["Ask for the next cell in plain words. It reads the schema, writes SQL or Python, and you insert it with one click."]),
    ], body_size=15)

    # 6 Assistant -------------------------------------------------------------------
    s = d.slide("Title Only", "Claude helps where you work, and you stay in control",
                notes="This is the takeaway slide for 'AI belongs where you work'. The assistant runs through your own Claude login, with no API key, and it is read-only by design.")
    d.row(s, T, 250, [
        dict(kind="acc", head="It sees what you see", body=["The chat panel in the SQL editor and the notebook reads the schema and runs read-only queries before it answers, so the SQL it writes uses your real table and column names."]),
        dict(kind="acc", head="It suggests, you run", body=["Answers come as SQL, Python or SAS code with an insert button. The assistant has no write access, no shell and no files. Nothing changes in the database until you run it."]),
        dict(kind="acc", head="It knows SingleStore", body=["Every question carries a SingleStore SQL skill with the pitfalls learned on real clusters: case-sensitive names, procedure syntax, shard and sort keys. You add to it as you learn."]),
    ], body_size=15)
    d.row(s, T + 270, 150, [
        dict(head="Three speeds", body=["Fast, Balanced or Thorough, picked per question. Each editor keeps one assistant running, so follow-ups are quick and keep the conversation."]),
        dict(head="Ask Claude everywhere", body=["The Schema Explorer and Pipeline Monitor hand a table, a skew finding or a pipeline error to the chat with the context already filled in."]),
    ], body_size=15)

    # 7 Operate (table) ---------------------------------------------------------------
    s = d.slide("Title Only", "Operate the cluster from the same rail",
                notes="Don't read the table. Pick two rows for the audience in the room: Schema and Pipelines for engineers, History and Security for DBAs.")
    rows = [
        ("View", "What it shows", "What you can do"),
        ("Schema", "Databases, tables with rows and size, columns, DDL with shard and sort keys, a row preview, data skew over partitions. SAS libraries and caslibs as sources too, badged by kind.", "Check skew, refresh statistics, query in grid, clean up the work tables SAS jobs leave behind."),
        ("Pipelines", "Every pipeline's state, source and target, progress such as files loaded or Kafka lag, latest batches and errors.", "Start, stop and test with confirmation; hand an error to Claude."),
        ("Cluster", "CPU, memory and disk per node, health and events, running queries, 7-day history, backups. Refreshes every 5 s.", "See a problem explained before it becomes a ticket."),
        ("History", "Every traced query with time, user, rows and a probable source: SAS CAS, SAS in-database, or a person.", "Group repeats, get tuning recommendations with the plan tree."),
        ("Alerts", "Long or failed queries, failed pipeline batches, memory and disk over a threshold, nodes not online.", "Edit the rules, acknowledge, clear."),
        ("Workload", "Saved sets of statements replayed with N connections; live throughput and latency.", "Compare two runs, for example before and after a new sort key."),
        ("Security", "Users with risk flags, effective privileges with the reason, who can access what, resource pools, audit settings.", "Each change previewed as SQL and confirmed, or saved as a script for the DBA."),
    ]
    tbl = s.Shapes.AddTable(len(rows), 3, L, T, W, 480).Table
    for c, wdt in enumerate((170, 672, 400), start=1):
        tbl.Columns(c).Width = wdt
    tbl.Rows(1).Height = 30
    for r, row in enumerate(rows, start=1):
        for c, val in enumerate(row, start=1):
            cell = tbl.Cell(r, c)
            tr = cell.Shape.TextFrame.TextRange
            tr.Text = val
            tr.Font.Size = 14 if r > 1 else 12
            tr.Font.Name = BOLD if (r == 1 or c == 1) else LIGHTF
            cell.Shape.TextFrame.MarginTop = cell.Shape.TextFrame.MarginBottom = 4

    # 8 SAS Viya built in ---------------------------------------------------------------
    s = d.slide("Title Only", "SAS is a source, a language and a file system in the Studio",
                notes="The badges on caslibs are a good teaching moment: SS means CAS pushes the query down to SingleStore live; S2 means the standard connector copies the table into memory.")
    d.row(s, T, 290, [
        dict(kind="viya", chip="Sources", head="SAS libraries and caslibs", body=["The Schema Explorer and the SQL editor's tree list SAS libraries from the Compute session and caslibs from CAS, next to SingleStore databases. Badges tell the kind apart: a SpeedyStore caslib that pushes down live, a standard connector that loads into memory, a SAS/ACCESS libname, a SAS 9 library over SAS/CONNECT."]),
        dict(kind="viya", chip="Languages", head="PROC SQL, FedSQL, CAS", body=["Run as picks the engine per tab. SAS cells in the notebook. One Compute and one CAS session per Viya connection, started in the background when the Studio opens so the first run doesn't wait."]),
        dict(kind="viya", chip="Files and sign-in", head="SAS Content and SAS server", body=["Open and save programs and notebooks where SAS Studio keeps them. Sign in with your own Viya account through SAS Logon; several saved Viya connections, one active, a tab can be pinned to another."]),
    ], body_size=14)
    d.note(s, T + 310, 70, "Why it matters.", "SAS SpeedyStore is SAS Viya and SingleStore together. The Studio shows both sides of every table "
           "and lets you choose, per query, where the work runs.")

    # 9 Benefits ---------------------------------------------------------------------------
    s = d.slide("Title Only", "What changes for the people who use it",
                notes="Tie back to the three takeaways: the assistant where you work, everything for SpeedyStore in one place, and you can start right away.")
    d.row(s, T, 250, [
        dict(kind="s2", chip="Analyst", head="From question to answer in one place", bullets=[
            "Ask in plain words, get SQL that uses the real schema, run it, chart it",
            "Notebook for the analysis, SAS cell when the method lives in SAS",
            "No copies of data on the laptop"]),
        dict(kind="viya", chip="Data engineer", head="Pipelines and pushdown you can see", bullets=[
            "Kafka and S3 loads created, tested and watched from the same window",
            "Try a query native and through SAS, and see what was pushed down",
            "Replay a workload before and after a schema change"]),
        dict(kind="acc", chip="DBA", head="Operate without surprises", bullets=[
            "Cluster health, alerts and query history with the SAS source identified",
            "Who can access what, with the reason",
            "Every change previewed as SQL and confirmed, or handed over as a script"]),
    ], body_size=15, head_size=18)
    d.note(s, T + 270, 70, "Common to all three:", "the assistant is read-only, every action that changes something is confirmed in the app, "
           "and the Studio works the same inside Claude and on its own.")

    # 10 How you run it ---------------------------------------------------------------------
    s = d.slide("Title Only", "Inside Claude, or on its own",
                notes="Say plainly that this is a pre-sales build on an open-source server, not a SAS product. Then show the shortcut: double-click, the Studio opens.")
    d.row(s, T, 260, [
        dict(kind="acc", chip="With Claude", head="An MCP server with apps", bullets=[
            "Runs from Claude Code, Claude Desktop or VS Code; the Studio opens in the chat or full-window in the browser",
            "Slash commands for the workspace, a notebook, pipeline health and a table report",
            "Claude can also use the tools directly: run SQL, create and start pipelines, describe tables",
            "Restart the server in seconds after a code change, without reconnecting"]),
        dict(kind="s2", chip="Without Claude", head="A desktop app", bullets=[
            "The same views in their own window from a desktop shortcut; one-command install on Windows",
            "Talks directly to SingleStore and SAS Viya; no Claude subscription or MCP client needed",
            "Only the chat panels need Claude; everything else works",
            "Other MCP clients get the tools with plain text results"]),
    ], body_size=14, head_size=18)
    d.row(s, T + 280, 150, [
        dict(head="Any SingleStore", body=["Self-managed clusters and Helios alike. Passwords in the OS credential store; Entra ID and SSO sign-in supported."]),
        dict(head="Official SDKs", body=["The MCP Python SDK, SingleStore's own Python client and the MCP Apps browser client. The project itself is glue."]),
        dict(head="Open source", body=["github.com/jonasliesas/singlestore-mcp-server, tests on every push. Version 0.10 added SAS."]),
    ], body_size=14)

    # 11 Demo -----------------------------------------------------------------------------------
    s = d.slide("Title Only", "Live demo: from a question to a result, without leaving the Studio", title_size=36,
                notes="Keep the demo to ten minutes. If anything fails live, the fallback is the recorded run; the four steps are the script either way.")
    d.row(s, T, 280, [
        dict(kind="acc", num=1, head="Ask", body=["A question in plain words in the Studio's panel. Claude reads the schema and answers with SQL that uses the real table and column names."], out="You see: the assistant working inside the editor, read-only."),
        dict(kind="s2", num=2, head="Run", body=["Insert the SQL and run it in SingleStore. Then switch Run as to PROC SQL and run the same query through SAS Viya."], out="You see: result, SAS log and what was pushed down."),
        dict(kind="viya", num=3, head="Explore", body=["Take the result into a notebook: a Python chart in the next cell, or a SAS cell reading the table through the S2 library."], out="You see: the data stays in the database."),
        dict(kind="", num=4, head="Operate", body=["Open Pipelines and Cluster: the load that fed the table, the batches behind it, and what the query cost the nodes."], out="You see: the whole platform from one rail."),
    ], body_size=14, head_size=18)
    d.note(s, T + 300, 70, "Same four steps, whatever the data.", "The dataset and the story are chosen closer to the event; "
           "the path from ask to operate is what the Studio is for.")

    # 12 Takeaways --------------------------------------------------------------------------------
    s = d.slide("Title Only", "Three things to remember",
                notes="These are the three takeaways from the session abstract, word for word, so the audience leaves with what they were promised.")
    d.row(s, T, 260, [
        dict(kind="acc", num=1, head="AI belongs where you work", body=["The Model Context Protocol brings an assistant into your SQL editor and notebook, with read-only access to SAS Viya and SAS SpeedyStore. It helps without taking over."]),
        dict(kind="s2", num=2, head="Everything for SAS SpeedyStore in one place", body=["Write and run SQL, work in notebooks, explore schemas, load data with pipelines and watch the cluster, with SAS Viya and CAS tables reachable from the same workspace."]),
        dict(kind="viya", num=3, head="You can start right away", body=["The server behind the Studio is open source, works with self-managed and cloud SingleStore, and runs from Claude Code, Claude Desktop, VS Code or as a desktop app."]),
    ], body_size=16, head_size=20)

    # 13 Closing ------------------------------------------------------------------------------------
    s = d.slide("Closing", "Talk to your data platform", title_size=60,
                notes="Questions. Offer the repository link and the architecture document for the people who want the detail.")
    s.Shapes.Placeholders(2).TextFrame.TextRange.Text = "github.com/jonasliesas/singlestore-mcp-server"

    return d.save(png=png)


if __name__ == "__main__":
    n = build(png="--png" in sys.argv)
    print(f"wrote {OUT} ({n} slides)" + (f", PNGs in {PNG_DIR}" if "--png" in sys.argv else ""))
