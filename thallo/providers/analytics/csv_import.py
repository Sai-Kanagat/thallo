"""CSV import: the floor above manual. A person uploads a CSV; the intake stages its bytes under a
ref, and the request names that ref in `refs`.

The header must have a `date` column (YYYY-MM-DD). Other columns are read only when their
normalised name is in METRICS; any other column is ignored, so a header is never a metric name.
An empty cell, a dash, `n/a` or anything that is not a finite number is missing for that day,
never zero. A literal `0` is zero. Rows outside the window are dropped.
"""
from __future__ import annotations

import csv
import io
import re
from decimal import Decimal
from typing import Any, ClassVar

from thallo.connections.errors import InvalidConnection, ProviderUnavailable
from thallo.connections.registry import NoSettings, ProviderContext, ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.contracts.testing.mocks import MockProvider
from thallo.providers.analytics.http_base import PullFailed, finish, in_window, number
from thallo.signals import context as staging

NAME = "csv_import"
MAX_BYTES = 5_000_000
METRICS = ("sessions", "users", "pageviews", "visitors", "clicks", "impressions", "ctr", "position",
           "conversions", "leads", "calls", "followers", "likes", "comments", "shares", "reach")


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")


def parse(data: bytes, req: c.AnalyticsRequest) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if len(data) > MAX_BYTES:
        raise PullFailed("the CSV is larger than 5 MB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise PullFailed("the CSV is not UTF-8") from None
    try:
        all_rows = list(csv.reader(io.StringIO(text)))
    except csv.Error:
        raise PullFailed("the CSV could not be read") from None
    if not all_rows:
        raise PullFailed("the CSV is empty")
    header, reader = all_rows[0], all_rows[1:]
    cols = [_norm(h) for h in header]
    if "date" not in cols:
        raise PullFailed("the CSV has no date column")
    di = cols.index("date")
    use = {i: m for i, m in enumerate(cols) if m in METRICS}
    rows: list[dict[str, Any]] = []
    blanks = 0
    raw_rows: list[list[str]] = []
    for line in reader:
        raw_rows.append(line)
        if len(line) <= di or not in_window(line[di].strip(), req):
            continue
        day = line[di].strip()
        for i, metric in use.items():
            v = number(line[i]) if i < len(line) else None
            if v is None:
                blanks += 1
                continue
            rows.append({"date": day, "metric": metric, "value": v, "untrusted": {}})
    raw = {"header": header, "rows": raw_rows, "ignored_columns": [h for i, h in enumerate(header)
                                                                     if i != di and i not in use]}
    return rows, raw


class CsvImport:
    capability: ClassVar[Capability] = Capability.ANALYTICS
    name = NAME
    mode = ConnectionMode.LOCAL

    def __init__(self, ctx: ProviderContext) -> None:
        self.ctx = ctx
        self.clock = ctx.clock

    def _upload(self, req: c.AnalyticsRequest) -> bytes | None:
        s = staging.current()
        if s is None or s.tenant_id != self.ctx.tenant_id:
            return None
        for ref in req.refs:
            if ref in s.uploads:
                return s.uploads[ref]
        return None

    def describe(self) -> c.ProviderInfo:
        return c.ProviderInfo(capability=self.capability, name=NAME, mode=self.mode,
                              limits={"metrics": list(METRICS), "max_bytes": MAX_BYTES},
                              licence_note="the tenant's own file", sends_externally=False)

    def health(self) -> c.Health:
        s = staging.current()
        staged_here = s is not None and s.tenant_id == self.ctx.tenant_id and bool(s.uploads)
        return c.Health(connected=staged_here, checked_at=self.clock.now(),
                        detail=None if staged_here else "no uploaded CSV is staged")

    def estimate(self, request: c.Request) -> c.Estimate:
        c.check_request(self, request)
        if request.tenant_id != self.ctx.tenant_id:
            raise InvalidConnection("this connection belongs to another tenant")
        assert isinstance(request, c.AnalyticsRequest)
        if self._upload(request) is None:
            raise ProviderUnavailable("no uploaded CSV is staged for this request")
        return c.Estimate(cost_eur=Decimal(0), seconds=0.5)

    def run(self, request: c.Request) -> c.RunResult:
        c.check_cap(self.estimate(request), request)
        assert isinstance(request, c.AnalyticsRequest)
        data = self._upload(request)
        assert data is not None
        rows, raw = parse(data, request)
        return c.RunResult(output=finish(request, rows, raw), cost_eur=Decimal(0),
                           provenance=c.Provenance(made_by="provider", provider=NAME, mode=self.mode,
                                                   made_at=self.clock.now()))

    def mock(self) -> MockProvider:
        return MockProvider(self.capability, self.clock, name=f"mock-{NAME}")


def create(ctx: ProviderContext) -> CsvImport:
    return CsvImport(ctx)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.ANALYTICS, NAME, ConnectionMode.LOCAL, create, settings_schema=NoSettings)
