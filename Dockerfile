# syntax=docker/dockerfile:1
#
# Telegram sounding bot (telegram_bot.py). Long-polls Telegram, so it needs
# no inbound port -- only outbound HTTPS (Telegram, NOAA S3, THREDDS,
# Wyoming, Open-Meteo, airportsapi).
#
#   docker build -t socal-soaring-bot .
#   docker run -e TELEGRAM_BOT_TOKEN=... -v soaring-data:/data socal-soaring-bot
#
# Published by .github/workflows/docker.yml to ghcr.io/<owner>/<repo>.

FROM python:3.14-slim-trixie AS deps

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

COPY requirements.txt /tmp/requirements.txt
# --only-binary for everything except reverse_geocoder, which only ships
# an sdist (pure Python) -- anything else falling back to a source build
# means a wheel is missing for this platform and should fail loudly.
#
# eccodeslib/eckitlib (hard deps of herbie-data) are removed again: eckitlib
# bundles its own libproj + libsqlite3, and with pyproj's copies in the
# same process, `import eccodes` before `import metpy.calc` (grib.py's
# order) dies with "double free or corruption". Without the wheel, the
# eccodes bindings fall back to the system libeccodes (runtime stage).
RUN pip install --only-binary=:all: --no-binary=reverse_geocoder \
        -r /tmp/requirements.txt \
    && pip uninstall -y eccodeslib eckitlib


FROM python:3.14-slim-trixie

# tzdata: Simple_Sounding converts through real IANA zones (ZoneInfo).
# libeccodes0: Debian's build has no eckit/PROJ, so it can't clash with
# pyproj (see the deps stage).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata libeccodes0 \
    && rm -rf /var/lib/apt/lists/*

# Fira Sans is the plots' font.family (Simple_Sounding.py); Debian doesn't
# package it, so pull it from google/fonts pinned to a commit.
ARG FONTS_REF=23e54b51ddffbc7713c583748e3bd86f62b1fa4a
ADD --checksum=sha256:c29556a2719bf613ef3d5e070e40d903a8965d9c081beca1375dc1e6e0f93c23 \
    https://raw.githubusercontent.com/google/fonts/${FONTS_REF}/ofl/firasans/FiraSans-Regular.ttf \
    /usr/share/fonts/truetype/fira/
ADD --checksum=sha256:a4d8e149ecdd4874a0726eb0af894488b3b31c423d6b0017c8f415ed1b795b45 \
    https://raw.githubusercontent.com/google/fonts/${FONTS_REF}/ofl/firasans/FiraSans-Bold.ttf \
    /usr/share/fonts/truetype/fira/
RUN chmod 644 /usr/share/fonts/truetype/fira/*.ttf

# MPLCONFIGDIR is created up front so it belongs to app even if something
# (e.g. Rosetta when cross-building on a Mac) makes ~/.cache as root first.
RUN useradd --uid 10001 --create-home --home-dir /home/app app \
    && install -d -o app -g app /home/app/.cache /home/app/.cache/matplotlib

COPY --from=deps /opt/venv /opt/venv

WORKDIR /app/bot
COPY *.py ./
COPY sites.tsv /app/sites.tsv

# MALLOC_ARENA_MAX: the GRIB fetch pool is 16 threads; without it glibc
# gives each its own malloc arena and RSS balloons (see telegram_bot.py).
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/app \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/home/app/.cache/matplotlib \
    MALLOC_ARENA_MAX=2 \
    SOUNDING_CACHE_DIR=/data/cache \
    SOUNDING_OUTPUT_DIR=/data/out \
    SOUNDING_SITES_FILE=/app/sites.tsv \
    ECCODES_PYTHON_USE_FINDLIBS=1

RUN mkdir -p /data/cache /data/out && chown -R app:app /data
VOLUME ["/data"]

USER 10001
# Import the bot's modules in its own order: catches native-library clashes
# like the eckit one above at build time, not on the first chat request.
# Then build matplotlib's font cache (~12 s that would otherwise land on
# every new pod's first start) and fail if Fira Sans isn't discoverable.
RUN python -c "import grib, Simple_Sounding, telegram_bot" \
    && python -c "from matplotlib import font_manager as fm; fm.findfont('Fira Sans', fallback_to_default=False)"

CMD ["python", "telegram_bot.py"]
