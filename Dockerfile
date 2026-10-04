# One image, two services (see docker-compose.yml): `workspace` (agent + API + UI) and
# `sandbox` (tools, RAG, MCP servers - no internet).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    LITELLM_LOCAL_MODEL_COST_MAP=True AIWORKSPACE_ROOT=/app

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

RUN useradd --uid 1000 --create-home agent
COPY --chown=root:root . .
RUN mkdir -p /app/runtime /sandbox && chown agent:agent /app/runtime /sandbox

USER agent
EXPOSE 8000 8100
CMD ["uvicorn", "aiworkspace.api:app", "--host", "0.0.0.0", "--port", "8000"]
