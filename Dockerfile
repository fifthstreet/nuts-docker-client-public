# One image, two jobs (see docker-compose.yml):
#   nuts-client  (default command "run")    unprivileged: watches the UPS, serves the dashboard, writes a request file
#   nuts-helper  (command "helper")         privileged, no network: powers the host off when it sees that request file
FROM python:3.12-alpine
WORKDIR /app
COPY nuts_client.py /app/
# /data = event log + saved settings (volume). /box = the drop-box shared with the helper (volume); owned by the client's uid so a
# fresh named volume is writable by it. busybox's nsenter (already in the base image) lets the helper reach the host's systemd.
RUN apk add --no-cache tzdata && adduser -D -u 10001 nuts && mkdir /data /box && chown nuts /data /box
USER nuts
EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/healthz', timeout=4).status == 200 else 1)"
ENTRYPOINT ["python", "-u", "/app/nuts_client.py"]
CMD ["run"]
