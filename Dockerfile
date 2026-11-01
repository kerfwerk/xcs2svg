# Distroless Python: no shell, no package manager, runs as user 65532 ("nonroot").
# Pinned by digest (multi-arch index for :nonroot, resolved 2026-10-31).
# To update: docker buildx imagetools inspect gcr.io/distroless/python3-debian12:nonroot
# (or: podman pull ... && podman image inspect ... --format '{{.Digest}}')
# and replace the sha256 value with the digest shown there.
# Podman only keeps HEALTHCHECK with: podman build --format docker ...
FROM gcr.io/distroless/python3-debian12:nonroot@sha256:7d1042ce588ab97019fe95c24ffca7bc5a82ccdac572511d5e09bda4435c89c5

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    MAX_UPLOAD_MB=25

WORKDIR /srv
COPY --chown=65532:65532 app/ /srv/app/

USER 65532:65532
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["python3", "-c", "import os, sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/healthz', timeout=3).status == 200 else 1)"]

ENTRYPOINT ["python3", "-m", "app.server"]
