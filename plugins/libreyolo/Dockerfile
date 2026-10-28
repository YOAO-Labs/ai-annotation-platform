# BASE_IMAGE must be a release-approved Python/CUDA runtime image pinned by digest.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
WORKDIR /app
COPY wheelhouse/ /wheelhouse/
RUN python -m pip install --no-index --find-links=/wheelhouse aap-libreyolo-plugin==0.1.0 && rm -rf /wheelhouse \
    && groupadd -g 10001 plugin && useradd -u 10001 -g 10001 -M plugin \
    && mkdir -p /var/lib/aap-libreyolo && chown 10001:10001 /var/lib/aap-libreyolo
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["aap-libreyolo", "serve", "--host", "0.0.0.0", "--port", "8000"]
