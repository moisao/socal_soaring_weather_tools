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
RUN pip install --only-binary=:all: --no-binary=reverse_geocoder \
        -r /tmp/requirements.txt


# grib-profile, the GRIB2 decoder grib.py and point_forecast.py run (rust/). No dependencies
# beyond std, so this needs no network; for arm64 it compiles under
# emulation, which a crate this small keeps short.
FROM rust:1.100-slim-trixie AS grib-profile

WORKDIR /src
COPY rust/Cargo.toml rust/Cargo.lock ./
COPY rust/src src
RUN cargo build --release --locked --offline \
    && cargo test --release --locked --offline


FROM python:3.14-slim-trixie

# tzdata: Simple_Sounding converts through real IANA zones (ZoneInfo).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
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
COPY --from=grib-profile /src/target/release/grib-profile /usr/local/bin/

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
    SOUNDING_SITES_FILE=/app/sites.tsv

RUN mkdir -p /data/cache /data/out && chown -R app:app /data
VOLUME ["/data"]

USER 10001
# Import the bot's modules in its own order: catches a missing package or
# a native-library clash at build time, not on the first chat request.
# Then build matplotlib's font cache (~12 s that would otherwise land on
# every new pod's first start) and fail if Fira Sans isn't discoverable.
RUN grib-profile --version \
    && python -c "import grib, Simple_Sounding, point_forecast, telegram_bot" \
    && python -c "from matplotlib import font_manager as fm; fm.findfont('Fira Sans', fallback_to_default=False)"

CMD ["python", "telegram_bot.py"]
