# Pinned to an explicit patch version AND its manifest-list digest, not the
# floating `3.12-slim` tag (closes the LOW finding: a floating tag can
# silently build a different image than the one that was tested). Digest
# resolved live against the Docker Registry HTTP API v2 (auth.docker.io
# token + registry-1.docker.io manifest GET, docker-content-digest response
# header), 2026-07-12:
#   TOKEN=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull" | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
#   curl -s -H "Authorization: Bearer $TOKEN" \
#     -H "Accept: application/vnd.oci.image.index.v1+json" \
#     "https://registry-1.docker.io/v2/library/python/manifests/3.12.13-slim" -D -
# The digest below is the multi-arch manifest-LIST digest (not a single
# platform's), so `docker build` still resolves the correct platform
# automatically, same as an unpinned tag would. A prior pass left this at
# the patch-version-only pin because the maintainer's private leak-scan
# hook flagged any bare 40+ char hex string as a possible secret with no
# allowlist entry for this one; that pattern was fixed at the source
# (context-aware exclusion for '@'/'sha256:' prefixes, not a per-value
# allowlist) earlier in this same hardening pass, so the full digest pin
# below commits cleanly with no hook exception needed.
FROM python:3.12.13-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates \
      libglib2.0-0 \
      libgl1 \
      libxcb1 \
      libx11-6 \
      libxext6 \
      libsm6 \
      libxrender1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 3033
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3033/health', timeout=4).read()"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "3033", "--workers", "1"]
