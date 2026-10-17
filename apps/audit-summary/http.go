package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
)

const (
	monthlySummaryPath = "/v1/audit/monthly-summary"
	maxRequestBytes    = 64 << 20
	// shortcut: 2 concurrent summaries, raise after measuring peak request memory.
	maxConcurrent = 2
)

type healthResponse struct {
	Service  string `json:"service"`
	Protocol int    `json:"protocol"`
}

type errorResponse struct {
	Code      string `json:"code"`
	Message   string `json:"message"`
	Retryable bool   `json:"retryable"`
}

type handler struct {
	slots chan struct{}
}

func newHandler() http.Handler {
	return &handler{slots: make(chan struct{}, maxConcurrent)}
}

func (h *handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	switch r.URL.Path {
	case "/health":
		if r.Method != http.MethodGet {
			methodNotAllowed(w, http.MethodGet)
			return
		}
		writeJSON(w, http.StatusOK, healthResponse{Service: "audit-summary", Protocol: 1})
	case "/openapi.json":
		if r.Method != http.MethodGet {
			methodNotAllowed(w, http.MethodGet)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		_, _ = w.Write(openAPISpec)
	case monthlySummaryPath:
		if r.Method != http.MethodPost {
			methodNotAllowed(w, http.MethodPost)
			return
		}
		h.monthlySummary(w, r)
	default:
		writeError(w, http.StatusNotFound, "not_found", "route not found", false)
	}
}

func (h *handler) monthlySummary(w http.ResponseWriter, r *http.Request) {
	select {
	case h.slots <- struct{}{}:
		defer func() { <-h.slots }()
	default:
		writeError(w, http.StatusServiceUnavailable, "service_overloaded", "summary capacity is full; retry later", true)
		return
	}

	limitedBody := http.MaxBytesReader(w, r.Body, maxRequestBytes)
	data, err := io.ReadAll(limitedBody)
	if err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			writeError(w, http.StatusRequestEntityTooLarge, "request_too_large", "request body exceeds 64 MiB", false)
			return
		}
		if r.Context().Err() != nil {
			return
		}
		writeError(w, http.StatusBadRequest, "invalid_request", "request body could not be read", false)
		return
	}
	input, err := decodeRequest(data)
	if err != nil {
		writeError(w, http.StatusUnprocessableEntity, "invalid_request", err.Error(), false)
		return
	}
	result, err := summarize(r.Context(), input)
	if err != nil {
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			return
		}
		writeError(w, http.StatusInternalServerError, "internal_error", "summary calculation failed", true)
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func methodNotAllowed(w http.ResponseWriter, method string) {
	w.Header().Set("Allow", method)
	writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "method not allowed", false)
}

func writeError(w http.ResponseWriter, status int, code, message string, retryable bool) {
	writeJSON(w, status, errorResponse{Code: code, Message: message, Retryable: retryable})
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
