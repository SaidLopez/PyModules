# PyModules Multi-Stage Dockerfile
# Optimized for production with security best practices

# =============================================================================
# Stage 1: Builder - Install dependencies and build the package
# =============================================================================
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build dependencies
RUN pip install --no-cache-dir hatchling build

# Copy only files needed for the build
COPY pyproject.toml README.md ./
COPY pymodules/ ./pymodules/

# Build the wheel
RUN python -m build --wheel --outdir /build/dist

# =============================================================================
# Stage 2: Runtime - Minimal production image
# =============================================================================
FROM python:3.12-slim AS runtime

# Security: Run as non-root user
RUN groupadd --gid 1000 pymodules \
    && useradd --uid 1000 --gid pymodules --shell /bin/bash --create-home pymodules

WORKDIR /app

# Install the built wheel
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

# Copy example application (optional - can be overridden by volume mount)
COPY --chown=pymodules:pymodules examples/ ./examples/

# Switch to non-root user
USER pymodules

# Environment variables (12-factor config)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYMODULES_LOG_LEVEL=INFO \
    PYMODULES_ENABLE_METRICS=true \
    PYMODULES_ENABLE_TRACING=false \
    PYMODULES_MAX_WORKERS=4

# Health check using Python
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import pymodules; print('healthy')" || exit 1

# Default command runs the demo
CMD ["python", "-m", "examples.demo"]

# Labels for container metadata
LABEL org.opencontainers.image.title="PyModules" \
      org.opencontainers.image.description="Event-driven modular architecture for Python" \
      org.opencontainers.image.version="0.3.0" \
      org.opencontainers.image.source="https://github.com/pymodules/pymodules" \
      org.opencontainers.image.licenses="MIT"
