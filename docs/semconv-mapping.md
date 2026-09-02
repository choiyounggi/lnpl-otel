# span ↔ OTel semconv mapping (definitive)

This table is the spec `lnpl_otel/exporter.py` implements against. Every row
is an actual enumeration of `impl/lnpl/interp.py` in linkly (the source of
every `name`/`kind`/`attrs` combination that can appear in a `trace_dict`),
never a guess — line numbers cite the checkout pinned in `pyproject.toml`.

## Span identity and kind

| linkly `kind` (`Span.kind`, `interp.py:315-333`) | Created at | OTel `SpanKind` | Notes |
|---|---|---|---|
| `"Workflow"` (root) | `interp.py:1467` | `SERVER` | One per `trace_dict["span"]`. |
| `"WorkflowStep"` | `interp.py:1525` (sequential), `interp.py:2143` (`parallel` block) | `INTERNAL` | Child of the root or of a `parallel` group. |
| `"Assignment"` | `interp.py:1712`, branch at `interp.py:1715` | `INTERNAL` | Effect child of a step. |
| `"Validation"` | `interp.py:1712`, branch at `interp.py:1800` | `INTERNAL` | |
| `"RepositoryCall"` (query) | `interp.py:1712`, branch at `interp.py:1802` | `INTERNAL` | `effect["operation"] == "query"`. |
| `"RepositoryCall"` (read/create/update/delete) | `interp.py:1712`, branch at `interp.py:1851` | `INTERNAL` | Every other `operation`. |
| `"CacheAccess"` | `interp.py:1712`, branch at `interp.py:1934` | `INTERNAL` | |
| `"Authorization"` | `interp.py:1712`, branch at `interp.py:1946` | `INTERNAL` | Phase 1: recorded, never enforced (`interp.py:1948-1959`). |
| `"NetworkCall"` | `interp.py:1712`, branch at `interp.py:1960` | **`CLIENT`** | The one kind that leaves the process — the only non-`INTERNAL`, non-root kind. |
| `"EventEmit"` | `interp.py:1712`, branch at `interp.py:2044` | `INTERNAL` | |
| `"Response"` | `interp.py:1712`, branch at `interp.py:2072` | `INTERNAL` | Declarative, no runtime work (`interp.py:2072-2078`). |
| `"Annotation"` | `interp.py:1712`, branch at `interp.py:2079` | `INTERNAL` | Declarative (`interp.py:2079-2085`). |

Every span also carries the linkly `name` (`interp.py:315` `Span.name`)
unchanged as the OTel span name — the root's is the workflow's own name; an
effect child's is `effect["id"].rsplit(".", 1)[-1]` (`interp.py:1712`).

## Attribute mapping

Every OTel span gets `lnpl.span.kind` = the original linkly `kind` string
verbatim (the table above), since OTel's `SpanKind` enum has only 5 values
and cannot carry linkly's finer-grained kind on its own.

| `trace_dict` / `span.attrs` source | linkly field | OTel attribute | Present on |
|---|---|---|---|
| `Trace.to_dict()["correlation_id"]` (`interp.py:370`) | — | `lnpl.correlation_id` | Root span only (it is a trace-level field, `Trace.correlation_id`, `interp.py:337-338`). |
| `span.attrs["attempts"]` (`interp.py:1544`, `interp.py:2169`) | step retry count | `lnpl.attempts` | `WorkflowStep` spans. |
| `span.attrs["target"]`/`["value"]` (`interp.py:1796-1797`) | Assignment | `lnpl.attr.target` / `lnpl.attr.value` | `Assignment` spans. |
| `span.attrs["row_count"]` (`interp.py:1845`) | RepositoryCall query | `lnpl.attr.row_count` | `RepositoryCall` (query) spans. |
| `span.attrs["found"]` (`interp.py:1864`) | RepositoryCall read/create/update/delete | `lnpl.attr.found` | `RepositoryCall` (execute) spans. |
| `span.attrs["ttl_ms"]` (`interp.py:1939`) | CacheAccess `set` | `lnpl.attr.ttl_ms` | `CacheAccess` spans (set op only). |
| `span.attrs["hit"]` (`interp.py:1941`) | CacheAccess `get` | `lnpl.attr.hit` | `CacheAccess` spans (get op only). CacheAccess `invalidate` sets neither attr. |
| `span.attrs["requirement"]` (`interp.py:1947`) | Authorization | `lnpl.attr.requirement` | `Authorization` spans. |
| `span.attrs["target"]`/`["status"]`/`["span_id"]` (`interp.py:1981`, `interp.py:2033`, `interp.py:2035`) | NetworkCall | `lnpl.attr.target` / `lnpl.attr.status` / `lnpl.attr.span_id` | `NetworkCall` spans. `status`/`span_id` only when the call is bound (`result`) / trace-propagated respectively. |
| `span.attrs["event"]`/`["emission_id"]` (`interp.py:2068-2069`) | EventEmit | `lnpl.attr.event` / `lnpl.attr.emission_id` | `EventEmit` spans. |

Any `span.attrs` key not named above (a future linkly effect kind's own
attrs) is still promoted generically as `lnpl.attr.<key>` — the table lists
every key that exists in the pinned linkly commit, not a closed enum the
exporter enforces. A non-primitive attribute value (not `str`/`bool`/`int`/
`float`) is JSON-encoded rather than dropped, since `span.attrs` is not
type-constrained the way OTel attributes are.

`lnpl.span.unfinished = true` is set (with `duration_ms` treated as `0`)
when a span's `duration_ms` is `None` (`interp.py:325-328` — an unterminated
span, `end_ms` never set). This attribute is `lnpl-otel`'s own, not a linkly
field.

## Timestamps

`interp.Span` never records absolute time — only a monotonic `duration_ms`
(`interp.py:87` `RealClock.now`, `interp.py:325-328` `Span.duration_ms`).
`OtlpExporter.export()` therefore *synthesizes* wall-clock start/end times at
export time rather than reproducing when things really happened:

- `anchor_end = time.time_ns()`, read once per `export()` call.
- Root: `end = anchor_end`, `start = anchor_end - duration_ms`.
- Each child stacks sequentially from its parent's `start`: child *i*'s
  `start` = parent `start` + the sum of durations of children `0..i-1`;
  `end` = that `start` + its own `duration_ms`.

These are approximations for display purposes (e.g. flame-graph shape) —
they do not claim to be the real wall-clock times the workflow ran at.

## Not exported

| Surface | Why not | Cite |
|---|---|---|
| `trace_dict["metrics"]` | Trace-only exporter (D9) — linkly already exposes metrics via its own `/metrics` Prometheus surface, so this package does not duplicate that as OTLP metrics. | `interp.py:372` (`Trace.to_dict`, `"metrics"` key), `interp.py:360-367` (`Trace.metric`) |
| `trace_dict["logs"]` | Same as above — trace-only exporter. | `interp.py:373` (`"logs"` key), `interp.py:356-358` (`Trace.log`) |
| `tracestate` | linkly never puts it in `to_dict()` at all — a vendor-extension field with PII risk that is deliberately kept off every exported surface, so `lnpl-otel` cannot see it even if it wanted to. | `interp.py:353` ("tracestate: never surfaced in `to_dict()`"), `interp.py:383-384` ("D10: tracestate is a vendor extension with PII risk — never surfaced in `to_dict()`, even when set.") |
