"""OtlpExporter — linkly's TraceExporter contract (`export(trace_dict)`,
`impl/lnpl/wsgi.py:171`), backed by the OTel Python SDK and shipped to an
OTLP collector.

Timestamp synthesis (D6): `interp.Span` never records absolute time, only
`duration_ms` (a monotonic-clock delta, `interp.py:87`), so this module
invents wall-clock timestamps at export time. `anchor_end` is `time.time_ns()`
read once per `export()` call; the root span's `end` is pinned there and its
`start` is `end - duration_ms`. Every child then stacks sequentially from its
parent's `start` (child i's start = parent start + sum of durations 0..i-1).
This is an approximation, not a re-derivation of when things really
happened — see docs/semconv-mapping.md.
"""

import atexit
import json
import logging
import os
import threading
import time
from contextlib import contextmanager

from lnpl.wsgi import ExporterError, TraceExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.id_generator import IdGenerator, RandomIdGenerator
from opentelemetry.trace import Link, SpanContext, SpanKind, TraceFlags, set_span_in_context

_DEFAULT_MAX_QUEUE_SIZE = 2048
_DEFAULT_SCHEDULE_DELAY_MILLIS = 5000
_DEFAULT_MAX_EXPORT_BATCH_SIZE = 512
_DEFAULT_EXPORT_TIMEOUT_MILLIS = 30000

_SAMPLED_REMOTE_FLAGS = TraceFlags(TraceFlags.SAMPLED)


def _build_span_exporter():
    """D4: protocol -> concrete OTLPSpanExporter. Endpoint is never passed
    explicitly — the SDK reads `OTEL_EXPORTER_OTLP_ENDPOINT` itself."""
    protocol = (os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL") or "grpc").strip()
    if protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        return OTLPSpanExporter()
    if protocol == "http/protobuf":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        return OTLPSpanExporter()
    raise ExporterError(
        "unknown OTEL_EXPORTER_OTLP_PROTOCOL %r (accepted: grpc, "
        "http/protobuf)" % protocol)


class _ReservingIdGenerator(IdGenerator):
    """D7: trace/span identity preservation. Delegates to `RandomIdGenerator`
    by default. `reserve(trace_id, span_id)` is a one-shot: the very next
    `generate_trace_id()`/`generate_span_id()` calls made while it is held
    (i.e. the ones `Tracer.start_span` makes while constructing the root
    span) return the reserved values instead of random ones, then the
    generator reverts to random for every span after that (children get
    fresh random span ids; a later `export()` call not holding a reservation
    is untouched).

    The lock is reentrant and covers every generate_*_id() call, not just
    the reserved ones: `start_span()` calls back into this generator while
    `reserve()` is still on the stack, and a *different* thread's concurrent,
    non-reserving `export()` must not be able to consume a reservation meant
    for the thread that is holding it.
    """

    def __init__(self):
        self._delegate = RandomIdGenerator()
        self._lock = threading.RLock()
        self._reserved_trace_id = None
        self._reserved_span_id = None

    def generate_trace_id(self):
        with self._lock:
            if self._reserved_trace_id is not None:
                value, self._reserved_trace_id = self._reserved_trace_id, None
                return value
            return self._delegate.generate_trace_id()

    def generate_span_id(self):
        with self._lock:
            if self._reserved_span_id is not None:
                value, self._reserved_span_id = self._reserved_span_id, None
                return value
            return self._delegate.generate_span_id()

    @contextmanager
    def reserve(self, trace_id=None, span_id=None):
        with self._lock:
            self._reserved_trace_id = trace_id
            self._reserved_span_id = span_id
            try:
                yield
            finally:
                self._reserved_trace_id = None
                self._reserved_span_id = None


class CountingBatchSpanProcessor(BatchSpanProcessor):
    """D8: the queue-saturation drop path is standard `BatchProcessor.emit`
    behavior (SDK 1.44, `opentelemetry.sdk._shared_internal.BatchProcessor`)
    — full queue -> drop + a warning log, nothing this package controls.
    This subclass only makes that already-happening drop *observable*:
    `on_end` peeks at the same queue/limit `emit()` is about to check and,
    under a lock, counts a drop before delegating to the real `on_end`.

    Reading `_batch_processor._queue`/`_max_queue_size` is a private-attr
    seam, not a public API — the whole point (per the plan) is that if a
    future SDK minor version moves this internal, the drop-inducing test
    that exercises this class goes red instead of the drop count silently
    going stale.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dropped = 0
        self._dropped_lock = threading.Lock()
        self._drop_tracking_enabled = True

    def on_end(self, span):
        if self._drop_tracking_enabled:
            try:
                sampled = bool(span.context and span.context.trace_flags.sampled)
                queue = self._batch_processor._queue
                max_size = self._batch_processor._max_queue_size
            except AttributeError:
                logging.getLogger(__name__).warning(
                    "CountingBatchSpanProcessor: BatchProcessor internals "
                    "moved (SDK update?) — disabling dropped_spans tracking",
                    exc_info=True)
                self._drop_tracking_enabled = False
            else:
                if sampled and len(queue) >= max_size:
                    with self._dropped_lock:
                        self.dropped += 1
        super().on_end(span)


def _coerce_attr_value(value):
    """OTel attribute values must be a primitive or a homogeneous sequence
    of one — `span.attrs` (interp.py `Span.attrs`) carries whatever a step
    computed, which is not guaranteed to be one. Anything else is
    JSON-encoded rather than dropped or left to raise."""
    if isinstance(value, (str, bool, int, float)):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _span_kind_for(is_root, linkly_kind):
    """D10: root is always SERVER; NetworkCall is the only CLIENT; every
    other step/effect is INTERNAL."""
    if is_root:
        return SpanKind.SERVER
    if linkly_kind == "NetworkCall":
        return SpanKind.CLIENT
    return SpanKind.INTERNAL


def _build_attributes(span_data, is_root, correlation_id, unfinished):
    """D10: `lnpl.*` namespace. `lnpl.span.kind` always carries the
    original linkly kind string (`"Workflow"`/`"WorkflowStep"`/an effect
    kind) since OTel's 5-value SpanKind enum cannot represent it.
    `attempts` gets the dedicated `lnpl.attempts` name (explicitly called
    out in D10); every other `span.attrs` key is promoted generically as
    `lnpl.attr.<key>`."""
    attrs = {"lnpl.span.kind": span_data.get("kind") or ""}
    if is_root and correlation_id is not None:
        attrs["lnpl.correlation_id"] = correlation_id
    for key, value in (span_data.get("attrs") or {}).items():
        if value is None:
            continue
        name = "lnpl.attempts" if key == "attempts" else "lnpl.attr.%s" % key
        attrs[name] = _coerce_attr_value(value)
    if unfinished:
        attrs["lnpl.span.unfinished"] = True
    return attrs


def _link_from_trace_dict(links):
    """`trace_dict["links"]` (`wsgi.py:1058-1086`) is a single dict —
    `{"trace_id": <32-hex>, "parent_id": <16-hex>}` — the untrusted inbound
    W3C context linkly chose not to adopt as this trace's own identity."""
    if not links:
        return None
    context = SpanContext(
        trace_id=int(links["trace_id"], 16),
        span_id=int(links["parent_id"], 16),
        is_remote=True,
        trace_flags=_SAMPLED_REMOTE_FLAGS)
    return [Link(context)]


class OtlpExporter(TraceExporter):
    """`lnpl.exporters` `otlp` registration's product. Owns a private
    `TracerProvider` + `BatchSpanProcessor` — never touches the global
    `opentelemetry.trace` tracer provider (a library must not clobber
    whatever the embedding process already set globally, D5).
    """

    def __init__(self, *, span_exporter=None,
                 max_queue_size=_DEFAULT_MAX_QUEUE_SIZE,
                 schedule_delay_millis=_DEFAULT_SCHEDULE_DELAY_MILLIS,
                 max_export_batch_size=_DEFAULT_MAX_EXPORT_BATCH_SIZE,
                 export_timeout_millis=_DEFAULT_EXPORT_TIMEOUT_MILLIS):
        service_name = (os.environ.get("OTEL_SERVICE_NAME") or "").strip()
        if not service_name:
            raise ExporterError(
                "OTEL_SERVICE_NAME is required for --trace-exporter otlp "
                "(unset or empty) — a failed launch, not a failed request")
        if span_exporter is None:
            span_exporter = _build_span_exporter()

        self._id_generator = _ReservingIdGenerator()
        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource, id_generator=self._id_generator)
        self._processor = CountingBatchSpanProcessor(
            span_exporter,
            max_queue_size=max_queue_size,
            schedule_delay_millis=schedule_delay_millis,
            max_export_batch_size=max_export_batch_size,
            export_timeout_millis=export_timeout_millis)
        provider.add_span_processor(self._processor)
        self._provider = provider
        self._tracer = provider.get_tracer("lnpl_otel")
        # D12: `TraceExporter` has no shutdown() hook (wsgi.py:171) — the
        # process exiting is the only lifecycle signal we get, so flush
        # whatever the BatchSpanProcessor is still holding then.
        atexit.register(provider.shutdown)

    @property
    def dropped_spans(self):
        return self._processor.dropped

    def force_flush(self, timeout_millis=_DEFAULT_EXPORT_TIMEOUT_MILLIS):
        return self._provider.force_flush(timeout_millis)

    def export(self, trace_dict):
        span_data = trace_dict.get("span")
        if span_data is None:
            return
        anchor_end_ns = time.time_ns()
        duration_ms = span_data.get("duration_ms") or 0
        root_start_ns = anchor_end_ns - int(duration_ms * 1_000_000)

        trace_id_hex = trace_dict.get("trace_id")
        span_id_hex = trace_dict.get("span_id")
        reserve_trace_id = int(trace_id_hex, 16) if trace_id_hex else None
        reserve_span_id = int(span_id_hex, 16) if span_id_hex else None
        links = _link_from_trace_dict(trace_dict.get("links"))
        correlation_id = trace_dict.get("correlation_id")

        self._export_span(
            span_data, parent_context=None, start_ns=root_start_ns,
            reserve_trace_id=reserve_trace_id, reserve_span_id=reserve_span_id,
            links=links, correlation_id=correlation_id, is_root=True)

    def _export_span(self, span_data, parent_context, start_ns,
                      reserve_trace_id, reserve_span_id, links,
                      correlation_id, is_root):
        duration_ms = span_data.get("duration_ms")
        unfinished = duration_ms is None
        duration_ms = 0 if unfinished else duration_ms
        end_ns = start_ns + int(duration_ms * 1_000_000)

        kind = _span_kind_for(is_root, span_data.get("kind"))
        attributes = _build_attributes(span_data, is_root, correlation_id, unfinished)
        start_kwargs = dict(
            name=span_data.get("name") or "", kind=kind, attributes=attributes,
            links=links or (), start_time=start_ns, context=parent_context)

        if reserve_trace_id is not None or reserve_span_id is not None:
            with self._id_generator.reserve(reserve_trace_id, reserve_span_id):
                span = self._tracer.start_span(**start_kwargs)
        else:
            span = self._tracer.start_span(**start_kwargs)
        span.end(end_time=end_ns)

        child_context = set_span_in_context(span)
        child_start_ns = start_ns
        for child in span_data.get("children") or []:
            self._export_span(
                child, parent_context=child_context, start_ns=child_start_ns,
                reserve_trace_id=None, reserve_span_id=None, links=None,
                correlation_id=None, is_root=False)
            child_duration_ms = child.get("duration_ms") or 0
            child_start_ns += int(child_duration_ms * 1_000_000)
