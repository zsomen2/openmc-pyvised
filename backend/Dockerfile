# syntax=docker/dockerfile:1
# PyVised backend image: OpenMC 0.16.0 (conda-forge) + ENDF/B-VII.1 + the PyVised HTTP service.
# Build from the repository root (see the justfile):
#   docker build -f backend/Dockerfile --target runtime -t pyvised-backend:dev .

ARG MICROMAMBA_IMAGE=mambaorg/micromamba:2.9.0
# Only the nuclear data (NNDC ENDF/B-VII.1 HDF5 library) is taken from the official image;
# its v0.16.0 tag was built from the 0.15.3 sources, so OpenMC itself comes from conda-forge.
ARG DATA_IMAGE=openmc/openmc:v0.16.0

FROM ${DATA_IMAGE} AS data

# OpenMC, Python and the nuclear data; shared by the runtime and test images
FROM ${MICROMAMBA_IMAGE} AS base
COPY --chown=$MAMBA_USER:$MAMBA_USER backend/environment.yml /tmp/environment.yml
RUN micromamba install --yes --name base --file /tmp/environment.yml \
 && micromamba clean --all --yes

COPY --from=data /root/nndc_hdf5 /opt/nuclear-data/endfb-vii.1
ENV OPENMC_CROSS_SECTIONS=/opt/nuclear-data/endfb-vii.1/cross_sections.xml \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HDF5_USE_FILE_LOCKING=FALSE \
    PYVISED_WORKSPACE=/workspace

USER root
RUN mkdir -p /workspace /data/custom && chown $MAMBA_USER:$MAMBA_USER /workspace
USER $MAMBA_USER
WORKDIR /workspace

# development only: test tools + the MCNP converter used to check the model rebuilds,
# installed before the sources so that code changes do not reinstall them
FROM base AS test
ARG MAMBA_DOCKERFILE_ACTIVATE=1
ARG MCNP_ADAPTER_REF=80fda5abeed22502e2f6e19e57a9b2a8762844a8
RUN micromamba install --yes --name base --channel conda-forge pytest httpx \
 && micromamba clean --all --yes \
 && pip install --no-cache-dir \
    "https://github.com/openmc-dev/openmc_mcnp_adapter/archive/${MCNP_ADAPTER_REF}.zip"
COPY --chown=$MAMBA_USER:$MAMBA_USER protocol/ /src/protocol/
COPY --chown=$MAMBA_USER:$MAMBA_USER backend/ /src/backend/
RUN pip install --no-cache-dir --no-deps /src/protocol /src/backend
# the example models of the app (Tools > Examples), checked by backend/tests/test_example_models.py
COPY --chown=$MAMBA_USER:$MAMBA_USER examples/ /src/examples/
ENTRYPOINT ["/usr/local/bin/_entrypoint.sh"]
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "/src/protocol/tests", "/src/backend/tests"]

FROM base AS runtime
LABEL org.opencontainers.image.title="PyVised backend" \
      org.opencontainers.image.description="OpenMC service of the PyVised desktop app" \
      org.opencontainers.image.source="https://github.com/zsomen2/openmc-pyvised" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"
# run the following RUN steps inside the activated conda environment
ARG MAMBA_DOCKERFILE_ACTIVATE=1
COPY --chown=$MAMBA_USER:$MAMBA_USER protocol/ /src/protocol/
COPY --chown=$MAMBA_USER:$MAMBA_USER backend/ /src/backend/
RUN pip install --no-cache-dir --no-deps /src/protocol /src/backend
# the app version this image belongs to (its tag on ghcr.io); after the RUN steps, so a new
# version does not invalidate their cache
ARG VERSION=dev
LABEL org.opencontainers.image.version="${VERSION}"
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD ["/usr/local/bin/_entrypoint.sh", "python", "-m", "pyvised_backend", "healthcheck"]
ENTRYPOINT ["/usr/local/bin/_entrypoint.sh", "python", "-m", "pyvised_backend", "serve", "--host", "0.0.0.0", "--port", "8765"]
