# Local stand-in for the Code Ocean capsule environment, to check codeocean/run before uploading.
# Mirrors the capsule setup in codeocean/README.md: Python 3.12.8, the same apt packages, the same
# post-install script, and the same pip pins. Used by `make codeocean-emulate`.
FROM python:3.12.8-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends redis-server make curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY codeocean/postInstall /tmp/postInstall
RUN cd / && bash /tmp/postInstall

COPY codeocean/pip-packages.txt /tmp/pip-packages.txt
RUN pip install --no-cache-dir -r /tmp/pip-packages.txt

ENV OPA_BIN=/opt/bin/opa OTELCOL_BIN=/opt/bin/otelcol-contrib
RUN mkdir -p /code /data /results /scratch
WORKDIR /code
CMD ["bash", "/code/run"]
