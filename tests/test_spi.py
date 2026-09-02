"""`lnpl.exporters` SPI resolution — `open_exporter("otlp")` against the real
installed entry-point (no mocking `entry_points()`). D11 ②.
"""

import os
import unittest
from unittest import mock

from lnpl.cli import _REJECTED, _open_trace_exporter
from lnpl.wsgi import ExporterError, StderrJsonExporter, open_exporter

from lnpl_otel.exporter import OtlpExporter


class OpenExporterSPITest(unittest.TestCase):
    def test_open_exporter_resolves_otlp_to_our_exporter_when_service_name_set(self):
        with mock.patch.dict(os.environ, {"OTEL_SERVICE_NAME": "spi-test-service"}):
            exporter = open_exporter("otlp")

        self.assertIsInstance(exporter, OtlpExporter)

    def test_open_exporter_raises_exporter_error_when_service_name_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ExporterError):
                open_exporter("otlp")

    def test_cli_open_trace_exporter_rejects_when_service_name_unset(self):
        # cli.py:1259 `_open_trace_exporter` translates (ValueError,
        # ExporterError) into the `_REJECTED` sentinel (rc 2), never lets it
        # escape — this is the actual `--trace-exporter otlp` startup path.
        with mock.patch.dict(os.environ, {}, clear=True):
            result = _open_trace_exporter("otlp")

        self.assertIs(_REJECTED, result)

    def test_stderr_json_is_not_shadowed_by_the_otlp_entry_point(self):
        # `stderr-json` is matched by string comparison ahead of the
        # entry-points loop (wsgi.py:339-340) — this proves it stays that
        # way even with `lnpl-otel` installed and registered.
        with mock.patch.dict(os.environ, {"OTEL_SERVICE_NAME": "spi-test-service"}):
            exporter = open_exporter("stderr-json")

        self.assertIsInstance(exporter, StderrJsonExporter)

    def test_unknown_exporter_name_raises_value_error(self):
        with self.assertRaises(ValueError):
            open_exporter("does-not-exist")


if __name__ == "__main__":
    unittest.main()
