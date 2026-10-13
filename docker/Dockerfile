# Build context: the repository root.
ARG BASE_IMAGE=ghcr.io/bioconductor/r-ver:RELEASE_3_23
FROM ${BASE_IMAGE}
ARG BASE_IMAGE

# Configure scripts that find libsbml5-dev of the base image add -I/usr/include ahead of the conda sysroot.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      bzip2 ca-certificates curl git python3 python3-venv xz-utils zstd \
 && apt-get purge -y libsbml5-dev \
 && rm -rf /var/lib/apt/lists/*

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/opt/biocpkgconda \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /opt/biocpkgconda
COPY requirements.txt ./
RUN python3 -m venv /opt/venv \
 && pip install --no-cache-dir --require-hashes --no-deps -r requirements.txt

COPY channel.toml overrides.toml sysreqs.toml ./
COPY biocpkgconda biocpkgconda/
RUN python -m biocpkgconda install-rattler-build --dest /usr/local/bin \
 && python -m biocpkgconda fetch-pinning --dest /opt/conda-build-config

# Writable for any --user, which need not exist in /etc/passwd.
RUN install -d -m 1777 /home/builder /home/builder/.cache
ENV HOME=/home/builder \
    RATTLER_CACHE_DIR=/home/builder/.cache/rattler

LABEL org.opencontainers.image.title=biocpkgconda-builder \
      org.opencontainers.image.description="Builds Bioconductor and CRAN conda packages with rattler-build" \
      org.opencontainers.image.source=https://github.com/almahmoud/biocpkgconda \
      org.opencontainers.image.url=https://github.com/almahmoud/biocpkgconda \
      org.opencontainers.image.licenses=MIT \
      org.opencontainers.image.authors=https://github.com/almahmoud/biocpkgconda \
      org.opencontainers.image.vendor=biocpkgconda \
      org.opencontainers.image.version=${BIOCONDUCTOR_VERSION} \
      org.opencontainers.image.base.name=${BASE_IMAGE}

CMD ["bash"]
