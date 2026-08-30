FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    CLOAKBROWSER_CACHE_DIR=/opt/cloakbrowser \
    CLOAK_EXTRA_ARGS="[\"--disable-crashpad-for-testing\",\"--single-process\"]" \
    HOME=/home/app

WORKDIR /app

# Playwright supplies the browser runtime; wireproxy is pinned and verified
# separately because it is the userspace WireGuard transport.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium

ARG WIREPROXY_VERSION=v1.1.3
ARG WIREPROXY_SHA256=e88c1d090740373fc606c1bafd81d9a5eadc642cce5667616e20e9d7a444f51c
RUN set -eux; \
    archive="$(mktemp)"; \
    curl --fail --location --proto '=https' --tlsv1.2 \
      "https://github.com/windtf/wireproxy/releases/download/${WIREPROXY_VERSION}/wireproxy_linux_amd64.tar.gz" \
      --output "${archive}"; \
    echo "${WIREPROXY_SHA256}  ${archive}" | sha256sum --check --strict; \
    extract_dir="$(mktemp -d)"; \
    tar --extract --gzip --file "${archive}" --directory "${extract_dir}"; \
    test -f "${extract_dir}/wireproxy"; \
    install --mode=0755 "${extract_dir}/wireproxy" /usr/local/bin/wireproxy; \
    rm -rf "${archive}" "${extract_dir}"

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin app \
    && mkdir -p /var/lib/turb /opt/cloakbrowser \
    && chown -R app:app /app /var/lib/turb /opt/cloakbrowser

# Pre-fetch Cloak's verified free Chromium into the image. The compose volume
# at /opt/cloakbrowser keeps this cache writable for an optional Pro license.
USER app
RUN python -m cloakbrowser install

USER root
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh
COPY . .
RUN set -eux; \
    for path in \
      accounts \
      codex_accounts \
      codex_agent_accounts \
      data \
      roxy_profile_archives \
      roxy_profile_staging \
      '注册日志' \
      turb.sqlite3 \
      '用于注册的邮箱.json' \
      '用于注册的邮箱.txt' \
      '用于注册的API邮箱.json' \
      '用于注册的API邮箱.txt' \
      '用于注册的Gmail API邮箱.json' \
      '用于注册的Gmail API邮箱.txt' \
      '用于注册的域名邮箱.json' \
      '注册成功的邮箱.json' \
      '注册成功的邮箱.txt' \
      '注册成功的token.txt' \
      '注册任务.json' \
      accounts_viewer.html \
      'codex_导出状态.json' \
      gmail_cdk_ledger.json \
      paymesh_card_ledger.json \
      outlook_accounts.txt \
      outlook_accounts_used.json; do \
        ln -s "/var/lib/turb/${path}" "/app/${path}"; \
    done; \
    chown -R app:app /app /var/lib/turb

USER app

EXPOSE 5057

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:5057", "--workers", "1", "--threads", "8", "--timeout", "0", "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]
