"""Unit tests for `OtlpExporter.export()` against an injected
`InMemorySpanExporter` — no network, no collector. D11 ①.
"""

import os
import threading
import time
import unittest
from unittest import mock

from lnpl.wsgi import ExporterError
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from lnpl_otel.exporter import OtlpExporter, _ReservingIdGenerator


def _span_by_name(spans, name):
    for span in spans:
        if span.name == name:
            return span
    raise AssertionError("no span named %r among %r" % (name, [s.name for s in spans]))


class OtlpExporterNormalCaseTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"OTEL_SERVICE_NAME": "test-service"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.span_exporter = InMemorySpanExporter()
        self.exporter = OtlpExporter(span_exporter=self.span_exporter)

    def _export_and_flush(self, trace_dict):
        self.exporter.export(trace_dict)
        self.exporter.force_flush()
        return self.span_exporter.get_finished_spans()

    def test_export_reconstructs_tree_ids_attrs_and_link(self):
        trace_id = "a" * 32
        span_id = "b" * 16
        link = {"trace_id": "c" * 32, "parent_id": "d" * 16}
        trace_dict = {
            "correlation_id": "corr-1",
            "trace_id": trace_id,
            "span_id": span_id,
            "links": link,
            "span": {
                "name": "CheckoutWorkflow", "kind": "Workflow", "duration_ms": 100,
                "attrs": {}, "children": [
                    {"name": "validate", "kind": "WorkflowStep", "duration_ms": 10,
                     "attrs": {"attempts": 1}, "children": []},
                    {"name": "charge-card", "kind": "WorkflowStep", "duration_ms": 40,
                     "attrs": {"attempts": 2}, "children": [
                         {"name": "stripe", "kind": "NetworkCall", "duration_ms": 30,
                          "attrs": {"target": "https://api.stripe.com", "status": 200},
                          "children": []},
                     ]},
                ],
            },
            "metrics": [], "logs": [],
        }

        spans = self._export_and_flush(trace_dict)

        self.assertEqual(4, len(spans))
        root = _span_by_name(spans, "CheckoutWorkflow")
        validate = _span_by_name(spans, "validate")
        step = _span_by_name(spans, "charge-card")
        call = _span_by_name(spans, "stripe")

        # trace identity preserved (D7): root gets exactly the given ids,
        # every span in the tree shares the inherited trace_id.
        self.assertEqual(trace_id, "%032x" % root.context.trace_id)
        self.assertEqual(span_id, "%016x" % root.context.span_id)
        self.assertEqual(root.context.trace_id, step.context.trace_id)
        self.assertEqual(root.context.trace_id, call.context.trace_id)
        self.assertNotEqual(root.context.span_id, step.context.span_id)

        # kind mapping (D10): root SERVER, NetworkCall CLIENT, else INTERNAL.
        self.assertEqual(SpanKind.SERVER, root.kind)
        self.assertEqual(SpanKind.INTERNAL, step.kind)
        self.assertEqual(SpanKind.CLIENT, call.kind)

        # attr mapping (D10).
        self.assertEqual("corr-1", root.attributes["lnpl.correlation_id"])
        self.assertEqual("Workflow", root.attributes["lnpl.span.kind"])
        self.assertEqual(2, step.attributes["lnpl.attempts"])
        self.assertEqual("https://api.stripe.com", call.attributes["lnpl.attr.target"])
        self.assertEqual(200, call.attributes["lnpl.attr.status"])

        # link (D7): the untrusted inbound context, as a Link on the root.
        self.assertEqual(1, len(root.links))
        self.assertEqual(int(link["trace_id"], 16), root.links[0].context.trace_id)
        self.assertEqual(int(link["parent_id"], 16), root.links[0].context.span_id)

        # timestamp synthesis (D6): child stacks sequentially from parent start.
        self.assertEqual(root.end_time - root.start_time, 100 * 1_000_000)
        # `validate` is root's first child: zero offset from root's start.
        self.assertEqual(validate.start_time, root.start_time)
        # `charge-card` is root's second child: offset by `validate`'s
        # duration (D6's sequential stacking, Sigma over preceding siblings).
        self.assertEqual(step.start_time, root.start_time + 10 * 1_000_000)
        # `call` is step's only (first) child: zero offset from step's start.
        self.assertEqual(call.start_time, step.start_time)

    def test_export_without_trace_identity_assigns_random_ids(self):
        trace_dict = {
            "correlation_id": "corr-2",
            "span": {"name": "run", "kind": "Workflow", "duration_ms": 5,
                     "attrs": {}, "children": []},
            "metrics": [], "logs": [],
        }

        spans = self._export_and_flush(trace_dict)

        self.assertEqual(1, len(spans))
        self.assertEqual((), spans[0].links)
        self.assertNotEqual(0, spans[0].context.trace_id)

    def test_export_with_non_json_scalar_attr_is_json_encoded(self):
        trace_dict = {
            "correlation_id": "corr-3",
            "span": {"name": "run", "kind": "Workflow", "duration_ms": 5, "attrs": {},
                     "children": [
                         {"name": "assign", "kind": "Assignment", "duration_ms": 1,
                          "attrs": {"value": {"nested": [1, 2]}}, "children": []},
                     ]},
            "metrics": [], "logs": [],
        }

        spans = self._export_and_flush(trace_dict)

        assign = _span_by_name(spans, "assign")
        self.assertIsInstance(assign.attributes["lnpl.attr.value"], str)
        self.assertIn("nested", assign.attributes["lnpl.attr.value"])


class OtlpExporterErrorCaseTest(unittest.TestCase):
    def test_missing_service_name_raises_exporter_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ExporterError):
                OtlpExporter(span_exporter=InMemorySpanExporter())

    def test_empty_service_name_raises_exporter_error(self):
        with mock.patch.dict(os.environ, {"OTEL_SERVICE_NAME": "   "}):
            with self.assertRaises(ExporterError):
                OtlpExporter(span_exporter=InMemorySpanExporter())

    def test_unknown_protocol_raises_exporter_error(self):
        env = {"OTEL_SERVICE_NAME": "test-service",
               "OTEL_EXPORTER_OTLP_PROTOCOL": "carrier-pigeon"}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(ExporterError):
                OtlpExporter()


class OtlpExporterBoundaryCaseTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"OTEL_SERVICE_NAME": "test-service"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.span_exporter = InMemorySpanExporter()
        self.exporter = OtlpExporter(span_exporter=self.span_exporter)

    def _export_and_flush(self, trace_dict):
        self.exporter.export(trace_dict)
        self.exporter.force_flush()
        return self.span_exporter.get_finished_spans()

    def test_root_span_none_exports_nothing(self):
        spans = self._export_and_flush(
            {"correlation_id": "corr", "span": None, "metrics": [], "logs": []})

        self.assertEqual(0, len(spans))

    def test_unfinished_root_duration_none_marks_span_unfinished(self):
        trace_dict = {
            "correlation_id": "corr",
            "span": {"name": "run", "kind": "Workflow", "duration_ms": None,
                     "attrs": {}, "children": []},
            "metrics": [], "logs": [],
        }

        spans = self._export_and_flush(trace_dict)

        self.assertEqual(1, len(spans))
        self.assertEqual(spans[0].start_time, spans[0].end_time)
        self.assertIs(True, spans[0].attributes["lnpl.span.unfinished"])

    def test_zero_children_exports_only_root(self):
        trace_dict = {
            "correlation_id": "corr",
            "span": {"name": "run", "kind": "Workflow", "duration_ms": 1,
                     "attrs": {}, "children": []},
            "metrics": [], "logs": [],
        }

        spans = self._export_and_flush(trace_dict)

        self.assertEqual(1, len(spans))

    def test_deeply_nested_children_all_exported_and_stacked(self):
        depth = 6
        node = {"name": "leaf", "kind": "Assignment", "duration_ms": 1,
                "attrs": {}, "children": []}
        for i in range(depth - 1, 0, -1):
            node = {"name": "level-%d" % i, "kind": "Assignment", "duration_ms": 1,
                    "attrs": {}, "children": [node]}
        trace_dict = {
            "correlation_id": "corr",
            "span": {"name": "root", "kind": "Workflow", "duration_ms": depth,
                     "attrs": {}, "children": [node]},
            "metrics": [], "logs": [],
        }

        spans = self._export_and_flush(trace_dict)

        self.assertEqual(depth + 1, len(spans))
        trace_ids = {s.context.trace_id for s in spans}
        self.assertEqual(1, len(trace_ids))

    def test_unicode_span_name_preserved(self):
        trace_dict = {
            "correlation_id": "corr",
            "span": {"name": "결제-워크플로우 🚀", "kind": "Workflow", "duration_ms": 1,
                     "attrs": {}, "children": []},
            "metrics": [], "logs": [],
        }

        spans = self._export_and_flush(trace_dict)

        self.assertEqual("결제-워크플로우 🚀", spans[0].name)


class OtlpExporterDropCounterTest(unittest.TestCase):
    def test_dropped_spans_counts_queue_saturation(self):
        with mock.patch.dict(os.environ, {"OTEL_SERVICE_NAME": "test-service"}):
            exporter = OtlpExporter(
                span_exporter=InMemorySpanExporter(),
                max_queue_size=1, schedule_delay_millis=60_000,
                max_export_batch_size=1)
            for i in range(5):
                exporter.export({
                    "correlation_id": "c%d" % i,
                    "span": {"name": "run", "kind": "Workflow", "duration_ms": 1,
                             "attrs": {}, "children": []},
                    "metrics": [], "logs": [],
                })
            time.sleep(0.2)

            self.assertGreater(exporter.dropped_spans, 0)


class ReservingIdGeneratorThreadSafetyTest(unittest.TestCase):
    def test_concurrent_reservations_never_cross_threads(self):
        # D7: the RLock must serialize `reserve()` itself, not just protect
        # individual field reads/writes — otherwise thread A's reservation
        # could be overwritten by thread B's before A's generate_*_id()
        # calls consume it. `time.sleep` between the two calls widens the
        # window a leaking lock would need to actually manifest as a bug.
        generator = _ReservingIdGenerator()
        start = threading.Barrier(2)
        mismatches = []

        def worker(trace_id, span_id):
            start.wait()
            with generator.reserve(trace_id, span_id):
                got_trace_id = generator.generate_trace_id()
                time.sleep(0.02)
                got_span_id = generator.generate_span_id()
            if (got_trace_id, got_span_id) != (trace_id, span_id):
                mismatches.append(
                    (trace_id, span_id, got_trace_id, got_span_id))

        threads = [threading.Thread(target=worker, args=(111, 222)),
                   threading.Thread(target=worker, args=(333, 444))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual([], mismatches)


if __name__ == "__main__":
    unittest.main()
