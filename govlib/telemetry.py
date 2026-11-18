"""OpenTelemetry setup shared by all services.

Pinned semantic conventions: opentelemetry-semantic-conventions 0.66b2 = semconv schema 1.44.0.
"""
from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.semconv.schemas import Schemas

SEMCONV_SCHEMA_URL = Schemas.V1_44_0.value

# GenAI attribute names (verified against opentelemetry.semconv._incubating.attributes.gen_ai_attributes)
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

_configured = False


def setup_tracing(service_name: str) -> trace.Tracer:
    global _configured
    if not _configured:
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}, schema_url=SEMCONV_SCHEMA_URL))
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        if endpoint:
            exporter = OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")
            provider.add_span_processor(
                BatchSpanProcessor(exporter, schedule_delay_millis=200, max_export_batch_size=512,
                                   max_queue_size=20000)
            )
        trace.set_tracer_provider(provider)
        _configured = True
    return trace.get_tracer(service_name, schema_url=SEMCONV_SCHEMA_URL)


def shutdown_tracing() -> None:
    provider = trace.get_tracer_provider()
    if hasattr(provider, "shutdown"):
        provider.shutdown()
