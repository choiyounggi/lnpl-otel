# lnpl-otel

OTLP `TraceExporter` for [lnpl](https://github.com/choiyounggi/linkly)'s
`TraceExporter` SPI ([linkly#144](https://github.com/choiyounggi/linkly/issues/144)):
turns a completed workflow `Trace` into an OTel span tree and ships it to any
OTLP collector (gRPC or HTTP/protobuf), verified against a real
otel-collector in CI via Testcontainers. Registered under the
`lnpl.exporters` entry-point group as `otlp`.

## Install

```
pip install lnpl-otel
```

This also installs a matching `lnpl` from PyPI. To try unreleased changes,
install from `main` instead:
`pip install git+https://github.com/choiyounggi/lnpl-otel@main`.

## Usage

Once installed, `lnpl` discovers this package's exporter automatically
through the `lnpl.exporters` entry-point:

```
lnpl serve --trace-exporter otlp
```

## Configuration

Configuration is entirely through the standard OTel SDK environment
variables — `--trace-exporter otlp` takes no `scheme:arg` suffix (the
`lnpl.exporters` SPI is unparameterized, unlike `lnpl.caches`/`lnpl.drivers`).

| Variable | Required | Default | Notes |
|---|---|---|---|
| `OTEL_SERVICE_NAME` | **Yes** | — | Unset or empty (`""`/whitespace-only) makes the exporter factory raise `lnpl.wsgi.ExporterError` at startup — `lnpl serve` exits with rc 2 rather than launching. A missing service name is a failed launch, not a failed request. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | No | SDK default (`http://localhost:4317`) | Read natively by the OTel SDK — this package never passes an endpoint explicitly. |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | No | `grpc` | `grpc` or `http/protobuf`. Any other value raises `ExporterError` at startup. |

Every other standard `OTEL_EXPORTER_OTLP_*` variable (headers, compression,
timeout, TLS certs, …) is honored too, since the SDK's own OTLP exporter
classes read them directly.

## What gets exported

See [`docs/semconv-mapping.md`](docs/semconv-mapping.md) for the full,
line-cited span/attribute mapping. Summary:

- linkly's `interp.Span` tree becomes an OTel span tree — root `SpanKind.SERVER`,
  `NetworkCall` steps `SpanKind.CLIENT`, everything else `SpanKind.INTERNAL`.
- `span.attrs` are promoted under the `lnpl.*` namespace (`lnpl.attempts`,
  `lnpl.attr.<key>`, `lnpl.correlation_id`, `lnpl.span.kind`).
- **Timestamps are synthesized**, not real: linkly's `Span` only ever records
  a `duration_ms` (its clock is monotonic, never epoch), so this exporter
  invents wall-clock start/end times by anchoring the root span's end to
  `time.time_ns()` at export time and stacking children sequentially from
  there. Treat exported timestamps as approximate shape, not ground truth.
- `trace_dict["metrics"]` and `["logs"]` are **not** exported — linkly
  already exposes metrics via its own `/metrics` Prometheus surface, and
  this package is trace-only. `tracestate` is never exported either, but for
  a different reason: linkly never puts it in `to_dict()` in the first place
  (vendor-extension/PII risk).
- Trace identity is preserved end-to-end: when `trace_dict` carries
  `trace_id`/`span_id` (an inbound `traceparent` linkly adopted, or one it
  minted itself), the root OTel span gets exactly those ids — this is what
  lets a linkly request join a distributed trace instead of starting a new
  disconnected one. An untrusted inbound context (`trace_dict["links"]`)
  becomes an OTel `Link` on the root span instead of the trace's own identity.

## Dropped spans

`OtlpExporter` batches through a `BatchSpanProcessor`
(`max_queue_size=2048`, `schedule_delay_millis=5000`,
`max_export_batch_size=512`, `export_timeout_millis=30000` — all overridable
via constructor kwargs, not env, since they are this package's own
operational tuning, not linkly's). If the queue saturates, the SDK drops the
newest span and logs a warning — standard OTel `BatchSpanProcessor`
behavior, not something this package can prevent. What this package adds is
**visibility**: `OtlpExporter.dropped_spans` is a running count of drops,
so an operator can alert on it instead of only noticing gaps after the fact.

## Lifecycle

`TraceExporter` has no `shutdown()` hook — `lnpl serve` never calls one. This
package registers `atexit.register(provider.shutdown)` itself so any
still-batched spans are flushed when the process exits. `OtlpExporter.force_flush(timeout_millis=30000)`
is also exposed publicly, primarily for tests that need to observe an export
synchronously.

## Local testing

Requires Docker (used by Testcontainers to spin up a real otel-collector).

```
python3.13 -m venv .venv
.venv/bin/pip install -e ".[test]"
OTEL_SERVICE_NAME=test-service TESTCONTAINERS_RYUK_DISABLED=true \
  .venv/bin/python -m unittest discover -s tests -v
```

On macOS with Docker Desktop, Testcontainers' Ryuk reaper container can fail
to start with a socket-mount error. If you hit that, disable Ryuk for the
run (CI's `ubuntu-latest` runners don't need this) — the command above
already does.

## Supported lnpl versions

This package depends on `lnpl>=0.9,<0.10` from PyPI, so any lnpl 0.9.x
release works.

The `linkly-sync` workflow checks two things on every PR:

- `latest-release` fails when PyPI has a newer lnpl that this range excludes
  (for example 0.10.0). Then widen the upper bound, run the suite, and release.
- `latest-main` runs the suite against linkly's `main`, so a break shows up
  before linkly releases it.

## Releasing

1. Set `version` in `pyproject.toml` and merge that to `main`.
2. Tag the merge commit `v<version>` and push the tag.
3. `release.yml` runs the suite, builds the sdist and wheel, and publishes them
   to PyPI through Trusted Publishing. The PyPI publisher is: project
   `lnpl-otel`, owner `choiyounggi`, repository `lnpl-otel`, workflow
   `release.yml`, environment `pypi`.

PyPI never accepts the same version twice. If the publish job fails for an
outside reason (PyPI outage, publisher not registered), re-run only that job
with `gh run rerun <run-id> --failed`. If the cause is in this repo, fix it on
`main` and release the next patch version. Never delete or re-push a tag.
