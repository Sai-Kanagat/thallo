"""Search queries in, real query clusters with no page out. Plain code, no model.

Input is the Search Console performance export (Queries, or Queries with Pages), uploaded by a
person: the WP-16 Search Console provider pulls day totals only, so no query text reaches Thallo
from the API yet. Query text is untrusted: it is compared and stored as data, never placed in an
instruction and never used as a key outside the normalised cluster key (lowercase letters, digits
and spaces only).

Guards against thin pages at scale (BUILD_PLAN module 28, QUALITY_BAR section 10):
- one cluster per group of queries that share their meaningful words, never one page per query;
- a cluster needs at least `min_impressions` and at least two meaningful words between its queries;
- a cluster a known site page already covers, or one that already has a page brief, is skipped;
- at most `max_pages` clusters per run, highest impressions first.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field

MAX_QUERY_CHARS = 200
MAX_ROWS = 5000
MIN_IMPRESSIONS = 50
MAX_PAGES_PER_RUN = 3
JOIN_SIMILARITY = 0.6  # Jaccard share of meaningful words a query needs with a cluster's seed to join it
COVER_SHARE = 0.6  # share of a cluster's words a known page must carry to count as covering it

STOPWORDS = frozenset("""
a an and are as at be by can do does for from how i in is it me my near of on or the to what when where which
who why with you your best cheap top
il lo la i gli le un uno una di da del della dei delle in con su per tra fra e o che come dove quando quale
quanto quanta quanti quante cosa chi mi ti si ci vicino a al alla ai alle
""".split())

HEADERS = {
    "query": ("top queries", "query", "queries", "query principali", "query piu frequenti"),
    "page": ("page", "pages", "top pages", "pagina", "pagine", "pagine principali"),
    "clicks": ("clicks", "clic"),
    "impressions": ("impressions", "impressioni"),
    "position": ("position", "posizione"),
}


@dataclass(frozen=True)
class QueryRow:
    query: str  # untrusted
    impressions: int
    clicks: int = 0
    position: float | None = None
    page: str | None = None  # untrusted


@dataclass(frozen=True)
class SitePage:
    """A page the site already has: its path and title, from the CMS connection or typed by a person."""

    path: str
    title: str


@dataclass
class Cluster:
    key: str  # normalised meaningful words, sorted, space separated
    words: frozenset[str]
    rows: list[QueryRow] = field(default_factory=list)

    @property
    def impressions(self) -> int:
        return sum(r.impressions for r in self.rows)

    @property
    def head(self) -> QueryRow:
        return max(self.rows, key=lambda r: (r.impressions, -len(r.query)))

    @property
    def queries(self) -> list[str]:
        return [r.query for r in sorted(self.rows, key=lambda r: -r.impressions)]


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def words(text: str) -> frozenset[str]:
    """Meaningful words: folded, stopwords dropped, a trailing plural s trimmed."""
    out = set()
    for w in re.findall(r"[a-z0-9]+", _fold(text)):
        if w in STOPWORDS or len(w) < 2:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return frozenset(out)


def _number(raw: str, *, integer: bool = False) -> float | None:
    """A count column (`integer`) reads 1,200 and 1.200 as thousands; a decimal column reads 9,5."""
    s = (raw or "").strip().replace("%", "").replace(" ", "")
    if not s:
        return None
    if integer:
        s = s.replace(",", "").replace(".", "") if re.fullmatch(r"\d{1,3}([.,]\d{3})+", s) else s
    if s.count(",") == 1 and "." not in s:
        s = s.replace(",", ".")
    s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_export(data: bytes) -> list[QueryRow]:
    """Rows from a Search Console CSV export (English or Italian headers). Rows that do not parse
    are dropped; a file with no query or impressions column raises ValueError."""
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    try:
        header = [_fold(h).strip() for h in next(reader)]
    except StopIteration:
        raise ValueError("the export is empty") from None
    col: dict[str, int] = {}
    for name, options in HEADERS.items():
        for i, h in enumerate(header):
            if h in options and name not in col:
                col[name] = i
    if "query" not in col or "impressions" not in col:
        raise ValueError("the export needs a query column and an impressions column")
    out: list[QueryRow] = []
    for row in reader:
        if len(out) >= MAX_ROWS:
            break
        if len(row) <= max(col.values()):
            continue
        q = row[col["query"]].strip()[:MAX_QUERY_CHARS]
        imp = _number(row[col["impressions"]], integer=True)
        if not q or imp is None or imp < 0:
            continue
        clicks = _number(row[col["clicks"]], integer=True) if "clicks" in col else None
        pos = _number(row[col["position"]]) if "position" in col else None
        page = row[col["page"]].strip()[:500] if "page" in col else None
        out.append(QueryRow(q, int(imp), int(clicks or 0), pos, page or None))
    return out


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cluster(rows: Iterable[QueryRow]) -> list[Cluster]:
    """Greedy, deterministic: queries by impressions (then text), each joins the first cluster
    whose seed words it shares enough of, else seeds a new one."""
    merged: dict[str, QueryRow] = {}
    for r in rows:
        k = _fold(r.query).strip()
        old = merged.get(k)
        merged[k] = r if old is None else QueryRow(old.query, old.impressions + r.impressions,
                                                   old.clicks + r.clicks, old.position, old.page or r.page)
    ordered = sorted(merged.values(), key=lambda r: (-r.impressions, _fold(r.query)))
    clusters: list[Cluster] = []
    for r in ordered:
        w = words(r.query)
        if not w:
            continue
        home = next((c for c in clusters if similarity(c.words, w) >= JOIN_SIMILARITY), None)
        if home is None:
            clusters.append(Cluster(key="", words=w, rows=[r]))
        else:
            home.rows.append(r)
    for c in clusters:
        common = frozenset.intersection(*(words(r.query) for r in c.rows)) or c.words
        c.key = " ".join(sorted(common))[:200].strip()
    return clusters


def covered_by(c: Cluster, pages: Sequence[SitePage]) -> SitePage | None:
    for p in pages:
        have = words(p.title) | words(p.path.replace("/", " ").replace("-", " "))
        if c.words and len(c.words & have) / len(c.words) >= COVER_SHARE:
            return p
    return None


@dataclass(frozen=True)
class Skipped:
    key: str
    reason: str


def pick(rows: Iterable[QueryRow], *, site_pages: Sequence[SitePage], existing_keys: Collection[str] = (),
         min_impressions: int = MIN_IMPRESSIONS, max_pages: int = MAX_PAGES_PER_RUN
         ) -> tuple[list[Cluster], list[Skipped]]:
    """The clusters that should become page briefs this run, and why every other one did not."""
    chosen: list[Cluster] = []
    skipped: list[Skipped] = []
    taken = set(existing_keys)
    for c in sorted(cluster(rows), key=lambda c: (-c.impressions, c.key)):
        all_words = frozenset().union(*(words(r.query) for r in c.rows))
        if c.impressions < min_impressions:
            skipped.append(Skipped(c.key, f"below {min_impressions} impressions"))
        elif len(all_words) < 2:
            skipped.append(Skipped(c.key, "too few meaningful words to be a real question"))
        elif c.key in taken:
            skipped.append(Skipped(c.key, "already has a page brief"))
        elif (p := covered_by(c, site_pages)) is not None:
            skipped.append(Skipped(c.key, f"covered by an existing page ({p.path})"))
        elif len(chosen) >= max_pages:
            skipped.append(Skipped(c.key, f"over the cap of {max_pages} pages per run"))
        else:
            chosen.append(c)
            taken.add(c.key)
    return chosen, skipped


def slug_for(c: Cluster, taken: Collection[str] = ()) -> str:
    """A path of letters and hyphens only (digits would trip the truth compiler in links)."""
    parts = [w for w in sorted(c.words, key=lambda w: c.head.query.casefold().find(w) % 1000)
             if w.isalpha()][:6] or ["page"]
    base = "/" + "-".join(parts)
    slug, extra = base, iter("abcdefghijklmnopqrstuvwxyz")
    while slug in taken:
        slug = f"{base}-{next(extra)}"
    return slug
