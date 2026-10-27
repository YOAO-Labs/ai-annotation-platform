FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/packages
COPY apps/_shared/filesystem_read ./filesystem_read
COPY apps/_shared/site_agent_contract ./site_agent_contract
COPY apps/storage-agent ./storage-agent
RUN pip install --no-cache-dir ./filesystem_read ./site_agent_contract ./storage-agent \
    && mkdir -p /var/lib/aap-storage-agent \
    && chown 65532:65532 /var/lib/aap-storage-agent \
    && chmod 0700 /var/lib/aap-storage-agent
USER 65532:65532
ENTRYPOINT ["aap-storage-agent"]
CMD ["--config", "/etc/aap-storage-agent/agent.json"]
