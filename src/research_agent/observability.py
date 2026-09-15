"""Application-owned traces contain operational metadata only, never source text or model reasoning."""

import json
import logging
import os
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

from research_agent import __version__

_configured = False


class MetadataExporter(SpanExporter):
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, spans):
        with self.path.open("a") as file:
            for span in spans:
                file.write(
                    json.dumps(
                        {
                            "trace_id": format(span.context.trace_id, "032x"),
                            "span_id": format(span.context.span_id, "016x"),
                            "name": span.name,
                            "start_time": span.start_time,
                            "end_time": span.end_time,
                            "attributes": dict(span.attributes or {}),
                        }
                    )
                    + "\n"
                )
        return SpanExportResult.SUCCESS


def configure():
    global _configured
    if _configured:
        return
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": "deep-research-agent", "service.version": __version__}
        )
    )
    if path := os.getenv("RESEARCH_TRACE_FILE"):
        provider.add_span_processor(SimpleSpanProcessor(MetadataExporter(path)))
    trace.set_tracer_provider(provider)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _configured = True
