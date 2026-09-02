"""OTLP TraceExporter factory for `lnpl.exporters` entry-point registration."""

from .exporter import OtlpExporter

__all__ = ["make_exporter", "OtlpExporter"]


def make_exporter():
    return OtlpExporter()
