"""Search and answer-engine pages (WP-27, BUILD_PLAN module 28).

    queries.py   Search Console export in, real query clusters with no page out (plain code)
    markup.py    the page format, HTML and JSON-LD derived in code, the JSON-LD validator
    checks.py    page code checks: answer first, citable facts, JSON-LD, internal links, readability
    draft.py     one cluster to one page draft through the truth library, left for the gate
    persist.py   one transaction, one page_brief row per cluster (migration 0036)
    export.py    files from an approved gate decision only (the manual floor for a CMS)
"""
from thallo.extensions.pages.draft import (
    PageInputs,
    PageOutcome,
    PageStatus,
    PageWriter,
    RegistryWriter,
    WriterReply,
    draft_page,
)
from thallo.extensions.pages.export import ExportRefused, export_files
from thallo.extensions.pages.persist import existing, write_page
from thallo.extensions.pages.queries import Cluster, QueryRow, SitePage, parse_export, pick, slug_for

__all__ = [
    "Cluster", "ExportRefused", "PageInputs", "PageOutcome", "PageStatus", "PageWriter", "QueryRow",
    "RegistryWriter", "SitePage", "WriterReply", "draft_page", "existing", "export_files", "parse_export", "pick",
    "slug_for", "write_page",
]
