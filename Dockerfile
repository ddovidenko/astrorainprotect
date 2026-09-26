FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY astrorainprotect ./astrorainprotect
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TZ=America/Chicago STATE_DIR=/state
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home app \
    && mkdir -p /state && chown app:app /state
COPY --from=build /install /usr/local
USER app
VOLUME ["/state"]
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-m", "astrorainprotect.healthcheck"]
CMD ["python", "-m", "astrorainprotect"]
