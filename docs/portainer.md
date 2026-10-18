# Portainer

A stack started with `docker compose` shows up in Portainer as "created outside of Portainer - control is limited", and Portainer cannot adopt it. To manage the app fully from Portainer, create it as a Portainer stack instead.

Portainer's web editor cannot build images, so build the image once on the Docker host first.

## Install

1. On the Docker host, from a clone of this repository, build the image with the version tag named in `docker-compose.portainer.yml` (the `image:` line, e.g. `0.7.0`):

   ```bash
   docker build -t nuts-docker-client:0.7.0 .
   ```
2. In Portainer: **Stacks > Add stack > Web editor**. Paste the contents of **`docker-compose.portainer.yml`** (identical to `docker-compose.yml` without the `build:` lines). Name the stack, add your settings under **Environment variables** (see [configuration](configuration.md); all are optional), and deploy.
3. Open `http://<host>:8099/` and continue with the [quick start](../README.md#quick-start) checks.

Use the same stack name as an earlier command-line install to reuse its volumes, and with them your settings.

## Update

1. `git pull` in your clone, then rebuild the image with the new version tag (same `docker build` command).
2. Paste the new `docker-compose.portainer.yml` into the stack and press **Update the stack**.

Settings, the switch position and the event log are kept in the volumes.
