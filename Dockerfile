FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY meta_ads_mcp/ ./meta_ads_mcp/

RUN pip install --no-cache-dir .

EXPOSE 8000

ENV MCP_TRANSPORT=streamable-http
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8000

# Railway overrides PORT at runtime; the server reads PORT first, then MCP_PORT
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:'+os.environ.get('PORT',os.environ.get('MCP_PORT','8000'))+'/health')" || exit 1

ENTRYPOINT ["python", "-m", "meta_ads_mcp"]
