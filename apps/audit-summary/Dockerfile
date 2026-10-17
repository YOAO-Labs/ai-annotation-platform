FROM golang:1.27.2-alpine AS build

WORKDIR /src
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /audit-summary .

FROM scratch
COPY --from=build /audit-summary /audit-summary
USER 65532:65532
EXPOSE 8091
HEALTHCHECK --interval=10s --timeout=5s --retries=5 CMD ["/audit-summary", "-healthcheck"]
ENTRYPOINT ["/audit-summary"]
CMD ["-listen", "0.0.0.0:8091"]
