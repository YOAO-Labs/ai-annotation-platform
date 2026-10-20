FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS dependencies
WORKDIR /opt/workspace-conformance
COPY apps/workspace/conformance/node/package.json apps/workspace/conformance/node/pnpm-lock.yaml ./
RUN corepack enable
RUN CI=1 pnpm install --ignore-workspace --frozen-lockfile --prod --ignore-scripts

FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32
WORKDIR /opt/workspace-conformance
COPY --from=dependencies /opt/workspace-conformance ./
COPY apps/workspace/conformance/node/provider.mjs ./
COPY apps/workspace/conformance/node/src ./src
USER node
ENV WORKSPACE_HOST=0.0.0.0 WORKSPACE_PORT=8173
EXPOSE 8173
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:8173/ready').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "provider.mjs"]
