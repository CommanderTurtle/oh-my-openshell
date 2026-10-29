# SPDX-FileCopyrightText: 2026 CommanderTurtle
# SPDX-License-Identifier: AGPL-3.0-only
FROM ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852
RUN apt-get update && apt-get install -y --no-install-recommends bash tmux ca-certificates curl git python3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /bin/bash sandbox \
    && mkdir -p /sandbox && chown 10001:10001 /sandbox
COPY policy.yaml /etc/openshell/policy.yaml
USER 10001:10001
WORKDIR /sandbox
ENV HOME=/home/sandbox TERM=xterm-256color
CMD ["sleep", "infinity"]
