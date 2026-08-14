"""The six review sections.

Each module renders one section and computes nothing: every figure it shows was
measured in `analyzer/` and arrived inside a FileAnalysisDocument or the single
ComparisonDocument. That separation is the reason two sections cannot disagree
about the same metric.

`tools` is the exception and is not a section: it holds the parts of the page that
act on a file rather than describe one - the tool bench opened from the viewer's own
menu, the expanded workspace and the editor.
"""
