"""Real-receipt verification against an otel-collector: export over the wire
(grpc and http/protobuf), then assert on what the collector actually wrote
out, not on what we think we sent. D11 ③.

The pinned image (`otel/opentelemetry-collector-contrib`) ships no shell —
`container.exec("cat ...")` is not viable against it (verified: `exec:
"cat"`/`"sh"` both fail with "executable file not found in $PATH"). This
polls the collector's `file` exporter output through a host bind mount
instead (`with_volume_mapping(..., mode="rw")`), which needs no shell in the
container at all — same completion predicate (content exists), different
plumbing to get there.
"""

import json
import os
import shutil
import time
import unittest
from pathlib import Path
from unittest import mock

from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from lnpl_otel.exporter import OtlpExporter

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_TMP_ROOT = Path(__file__).resolve().parents[1] / ".claude" / "tmp"
_COLLECTOR_IMAGE = "otel/opentelemetry-collector-contrib:0.128.0"
_POLL_TIMEOUT_S = 30


def _fixture_trace_dict(correlation_id, trace_id, span_id, link):
    return {
        "correlation_id": correlation_id,
        "trace_id": trace_id,
        "span_id": span_id,
        "links": link,
        "span": {
            "name": "CollectorProbeWorkflow", "kind": "Workflow", "duration_ms": 50,
            "attrs": {}, "children": [
                {"name": "charge", "kind": "WorkflowStep", "duration_ms": 20,
                 "attrs": {"attempts": 3}, "children": []},
            ],
        },
        "metrics": [{"name": "step.duration_ms", "labels": {}, "value": 20}],
        "logs": [{"level": "INFO", "message": "ok", "correlation_id": correlation_id}],
    }


def _poll_for_line_containing(path, needle, timeout_s=_POLL_TIMEOUT_S):
    deadline = time.time() + timeout_s
    last_size = -1
    while time.time() < deadline:
        if path.exists():
            content = path.read_text()
            last_size = len(content)
            for line in content.splitlines():
                if needle in line:
                    return json.loads(line)
        time.sleep(0.5)
    raise AssertionError(
        "collector output at %s never contained %r within %ss (last size seen: %s)"
        % (path, needle, timeout_s, last_size))


class _CollectorReceiptTestCase(unittest.TestCase):
    """Base: one otel-collector container per test method, receiving both
    grpc (4317) and http/protobuf (4318) — subclasses pick which port to
    exercise so each test is a clean, isolated wire round-trip."""

    def setUp(self):
        self._out_dir = _TMP_ROOT / ("collector-out-%s" % self._testMethodName)
        if self._out_dir.exists():
            shutil.rmtree(self._out_dir)
        self._out_dir.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self._out_dir, ignore_errors=True)

        self._container = DockerContainer(_COLLECTOR_IMAGE)
        self._container.with_volume_mapping(
            str(_FIXTURES / "otel-collector-config.yaml"),
            "/etc/otelcol-contrib/config.yaml", mode="ro")
        self._container.with_volume_mapping(str(self._out_dir), "/traces", mode="rw")
        self._container.with_exposed_ports(4317, 4318)
        self._container.waiting_for(
            LogMessageWaitStrategy("Everything is ready")
            .with_startup_timeout(_POLL_TIMEOUT_S))
        self._container.start()
        self.addCleanup(self._container.stop)

    def _export_via(self, protocol, container_port, service_name):
        host = self._container.get_container_host_ip()
        port = self._container.get_exposed_port(container_port)
        env = {
            "OTEL_SERVICE_NAME": service_name,
            "OTEL_EXPORTER_OTLP_PROTOCOL": protocol,
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://%s:%s" % (host, port),
        }
        trace_id = "1" * 32
        span_id = "2" * 16
        link = {"trace_id": "3" * 32, "parent_id": "4" * 16}
        correlation_id = "corr-%s" % protocol.replace("/", "-")

        with mock.patch.dict(os.environ, env):
            exporter = OtlpExporter()
            exporter.export(_fixture_trace_dict(correlation_id, trace_id, span_id, link))
            self.assertTrue(exporter.force_flush(timeout_millis=_POLL_TIMEOUT_S * 1000))

        record = _poll_for_line_containing(self._out_dir / "out.json", correlation_id)
        return record, trace_id, span_id, link, service_name


class GrpcReceiptTest(_CollectorReceiptTestCase):
    def test_export_over_grpc_is_received_by_the_collector(self):
        record, trace_id, span_id, link, service_name = self._export_via(
            "grpc", 4317, "grpc-probe-service")

        resource_spans = record["resourceSpans"][0]
        resource_attrs = {a["key"]: a["value"].get("stringValue")
                          for a in resource_spans["resource"]["attributes"]}
        self.assertEqual(service_name, resource_attrs["service.name"])

        spans = resource_spans["scopeSpans"][0]["spans"]
        names = {s["name"] for s in spans}
        self.assertEqual({"CollectorProbeWorkflow", "charge"}, names)

        root = next(s for s in spans if s["name"] == "CollectorProbeWorkflow")
        self.assertEqual(trace_id, root["traceId"])
        self.assertEqual(span_id, root["spanId"])
        self.assertEqual(link["trace_id"], root["links"][0]["traceId"])
        self.assertEqual(link["parent_id"], root["links"][0]["spanId"])

        step = next(s for s in spans if s["name"] == "charge")
        step_attrs = {a["key"]: a["value"] for a in step["attributes"]}
        self.assertEqual("3", step_attrs["lnpl.attempts"]["intValue"])


class HttpProtobufReceiptTest(_CollectorReceiptTestCase):
    def test_export_over_http_protobuf_is_received_by_the_collector(self):
        record, trace_id, span_id, link, service_name = self._export_via(
            "http/protobuf", 4318, "http-probe-service")

        resource_spans = record["resourceSpans"][0]
        resource_attrs = {a["key"]: a["value"].get("stringValue")
                          for a in resource_spans["resource"]["attributes"]}
        self.assertEqual(service_name, resource_attrs["service.name"])

        spans = resource_spans["scopeSpans"][0]["spans"]
        names = {s["name"] for s in spans}
        self.assertEqual({"CollectorProbeWorkflow", "charge"}, names)

        root = next(s for s in spans if s["name"] == "CollectorProbeWorkflow")
        self.assertEqual(trace_id, root["traceId"])
        self.assertEqual(span_id, root["spanId"])
        self.assertEqual(link["trace_id"], root["links"][0]["traceId"])
        self.assertEqual(link["parent_id"], root["links"][0]["spanId"])


if __name__ == "__main__":
    unittest.main()
