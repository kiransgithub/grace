# Build context: repository root. Pin approved base-image digests before promotion.
FROM python:3.12-slim AS builder
WORKDIR /build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY pyproject.toml README.md ./
COPY proto ./proto
COPY src ./src
RUN pip install grpcio-tools==1.78.0 \
    && python -m grpc_tools.protoc -I proto --python_out=src --grpc_python_out=src proto/grace/v1/grace.proto \
    && pip install . \
    && pip uninstall -y grpcio-tools

FROM python:3.12-slim AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GRACE_MODE=simulation \
    GRACE_STATE_BACKEND=memory \
    GRACE_ENVIRONMENT=dev \
    GRACE_ALLOW_PRODUCTION=false \
    GRACE_HTTP_HOST=0.0.0.0 \
    GRACE_HTTP_PORT=8080 \
    GRACE_GRPC_HOST=0.0.0.0 \
    GRACE_GRPC_PORT=50051 \
    GRACE_GRPC_ENABLED=true
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
USER 10001:10001
EXPOSE 8080 50051
STOPSIGNAL SIGTERM
CMD ["python", "-m", "grace.transport.server"]
