# Build the wheel and its dependencies into a throwaway venv, then copy only
# that venv into the runtime image — pip, build tools and source trees never
# reach the shipped layer.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /src

# README.md is required: pyproject declares it as the project readme, so the
# hatchling build fails without it.
COPY pyproject.toml README.md ./
COPY src ./src

# The [http] extra pulls in uvicorn, needed for streamable-http / sse.
RUN pip install --upgrade pip && pip install ".[http]"


FROM python:3.12-slim

LABEL org.opencontainers.image.title="jenkins-mcp" \
      org.opencontainers.image.description="MCP server that triggers a Jenkins automation job and returns its result" \
      org.opencontainers.image.source="https://github.com/dittops/jenkins-job-mcp"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Fixed uid so the Kubernetes securityContext can pin runAsUser to the same
# value without depending on image internals.
RUN useradd --system --uid 10001 --user-group --no-create-home \
        --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY actions.json /app/actions.json

# Container defaults differ from the local ones on purpose: a container is
# reached over the network, and binding 127.0.0.1 inside a pod would be
# unreachable from anywhere else. The server has NO authentication of its own —
# keep it on a private network behind an authenticating proxy.
ENV MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    MCP_PATH=/mcp \
    JENKINS_ACTIONS_CONFIG=/app/actions.json

EXPOSE 8000
USER 10001

# The MCP endpoint rejects a plain GET (it needs the session/Accept headers), so
# probe the listener itself. Skipped when running as a stdio server, which has
# no port to check.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import os,socket,sys; t=os.environ.get('MCP_TRANSPORT','streamable-http'); sys.exit(0) if t=='stdio' else socket.create_connection(('127.0.0.1',int(os.environ.get('MCP_PORT','8000'))),2).close()"]

ENTRYPOINT ["jenkins-mcp"]
