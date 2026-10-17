package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestGoldenMonthlySummaryMatchesPythonReducerContract(t *testing.T) {
	requestBytes, err := os.ReadFile("testdata/mixed.request.json")
	if err != nil {
		t.Fatal(err)
	}
	wantBytes, err := os.ReadFile("testdata/mixed.response.json")
	if err != nil {
		t.Fatal(err)
	}
	httpResponse := serve(newHandler(), http.MethodPost, monthlySummaryPath, bytes.NewReader(requestBytes))
	if httpResponse.Code != http.StatusOK {
		t.Fatalf("monthly summary status = %d; body=%s", httpResponse.Code, httpResponse.Body.String())
	}
	var got any
	if err := json.Unmarshal(httpResponse.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	assertJSONEqual(t, got, wantBytes)
}

func TestSumBeyondInputWidthRemainsExact(t *testing.T) {
	maxInput := strings.Repeat("9", 38)
	input := request{Month: "2024-01", Rows: []row{
		{Day: "2024-01-01", Action: "a", StatusFamily: 5, EventCount: maxInput},
		{Day: "2024-01-01", Action: "a", StatusFamily: 2, EventCount: maxInput},
	}}
	got, err := summarize(context.Background(), input)
	if err != nil {
		t.Fatal(err)
	}
	wantTotal := "1" + strings.Repeat("9", 37) + "8"
	if got.Totals.EventCount != wantTotal || got.Totals.ErrorCount != maxInput || got.Daily[0].EventCount != wantTotal || got.TopActions[0].EventCount != wantTotal {
		t.Fatalf("large sum lost precision: got=%+v want total=%s", got.Totals, wantTotal)
	}
}

func TestStrictRequestValidation(t *testing.T) {
	valid := `{"month":"2024-02","materialized_through":null,"rows":[]}`
	cases := map[string]string{
		"missing required month":    `{"materialized_through":null,"rows":[]}`,
		"missing nullable field":    `{"month":"2024-02","rows":[]}`,
		"missing rows":              `{"month":"2024-02","materialized_through":null}`,
		"unknown top-level field":   `{"month":"2024-02","materialized_through":null,"rows":[],"extra":true}`,
		"trailing JSON value":       valid + ` {}`,
		"invalid month":             `{"month":"2024-13","materialized_through":null,"rows":[]}`,
		"zero year":                 `{"month":"0000-01","materialized_through":null,"rows":[]}`,
		"null rows":                 `{"month":"2024-02","materialized_through":null,"rows":null}`,
		"null materialized date":    valid,
		"wrong month day":           `{"month":"2024-02","materialized_through":null,"rows":[{"day":"2024-03-01","action":"a","target_type":"","actor_role":"","status_family":2,"event_count":"1"}]}`,
		"invalid leap day":          `{"month":"2023-02","materialized_through":null,"rows":[{"day":"2023-02-29","action":"a","target_type":"","actor_role":"","status_family":2,"event_count":"1"}]}`,
		"missing empty label":       `{"month":"2024-02","materialized_through":null,"rows":[{"day":"2024-02-01","action":"a","target_type":"","status_family":2,"event_count":"1"}]}`,
		"null label":                `{"month":"2024-02","materialized_through":null,"rows":[{"day":"2024-02-01","action":"a","target_type":null,"actor_role":"","status_family":2,"event_count":"1"}]}`,
		"numeric count":             `{"month":"2024-02","materialized_through":null,"rows":[{"day":"2024-02-01","action":"a","target_type":"","actor_role":"","status_family":2,"event_count":1}]}`,
		"noncanonical count":        `{"month":"2024-02","materialized_through":null,"rows":[{"day":"2024-02-01","action":"a","target_type":"","actor_role":"","status_family":2,"event_count":"01"}]}`,
		"count beyond 38 digits":    `{"month":"2024-02","materialized_through":null,"rows":[{"day":"2024-02-01","action":"a","target_type":"","actor_role":"","status_family":2,"event_count":"100000000000000000000000000000000000000"}]}`,
		"unsupported status family": `{"month":"2024-02","materialized_through":null,"rows":[{"day":"2024-02-01","action":"a","target_type":"","actor_role":"","status_family":1,"event_count":"1"}]}`,
		"fractional status family":  `{"month":"2024-02","materialized_through":null,"rows":[{"day":"2024-02-01","action":"a","target_type":"","actor_role":"","status_family":2.0,"event_count":"1"}]}`,
		"row unknown field":         `{"month":"2024-02","materialized_through":null,"rows":[{"day":"2024-02-01","action":"a","target_type":"","actor_role":"","status_family":2,"event_count":"1","extra":1}]}`,
		"row not an object":         `{"month":"2024-02","materialized_through":null,"rows":["row"]}`,
		"invalid JSON":              `{"month":`,
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			if name == "null materialized date" {
				input, err := decodeRequest([]byte(body))
				if err != nil || input.MaterializedThrough != nil {
					t.Fatalf("explicit null should be accepted: input=%+v err=%v", input, err)
				}
				return
			}
			if _, err := decodeRequest([]byte(body)); err == nil {
				t.Fatalf("decodeRequest(%s) unexpectedly succeeded", body)
			}
		})
	}
}

func TestRankingLimitAndStableTieBreak(t *testing.T) {
	input := `{"month":"2024-01","materialized_through":null,"rows":[`
	rows := make([]string, 12)
	for index := range rows {
		rows[index] = fmt.Sprintf(`{"day":"2024-01-01","action":"action-%02d","target_type":"type-%02d","actor_role":"role-%02d","status_family":2,"event_count":"1"}`, 11-index, index, index)
	}
	input += strings.Join(rows, ",") + "]}"
	decoded, err := decodeRequest([]byte(input))
	if err != nil {
		t.Fatal(err)
	}
	got, err := summarize(context.Background(), decoded)
	if err != nil {
		t.Fatal(err)
	}
	if len(got.TopActions) != 10 || got.TopActions[0].Key != "action-00" || got.TopActions[9].Key != "action-09" {
		t.Fatalf("top actions were not limited and key-sorted on ties: %+v", got.TopActions)
	}
	if len(got.TargetTypes) != 12 || got.TargetTypes[0].Key != "type-00" || got.TargetTypes[11].Key != "type-11" {
		t.Fatalf("target types should retain all tied keys in order: %+v", got.TargetTypes)
	}
}

func TestHTTPRoutesErrorsCapacityAndOpenAPI(t *testing.T) {
	handler := &handler{slots: make(chan struct{}, maxConcurrent)}

	response := serve(handler, http.MethodGet, "/health", nil)
	if response.Code != http.StatusOK || response.Body.String() != "{\"service\":\"audit-summary\",\"protocol\":1}\n" {
		t.Fatalf("health response: status=%d body=%s", response.Code, response.Body.String())
	}
	response = serve(handler, http.MethodGet, "/openapi.json", nil)
	if response.Code != http.StatusOK || !bytes.Equal(response.Body.Bytes(), openAPISpec) {
		t.Fatalf("OpenAPI response must be the embedded canonical file")
	}
	response = serve(handler, http.MethodPost, "/v1/audit/monthly-summary", strings.NewReader("{"))
	assertErrorResponse(t, response, http.StatusUnprocessableEntity, "invalid_request", false)
	response = serve(handler, http.MethodGet, "/missing", nil)
	assertErrorResponse(t, response, http.StatusNotFound, "not_found", false)
	response = serve(handler, http.MethodGet, "/health", nil)
	if response.Code != http.StatusOK {
		t.Fatalf("health should remain available independently of summary capacity")
	}
	response = serve(handler, http.MethodPost, "/health", nil)
	assertErrorResponse(t, response, http.StatusMethodNotAllowed, "method_not_allowed", false)

	for range maxConcurrent {
		handler.slots <- struct{}{}
	}
	response = serve(handler, http.MethodPost, monthlySummaryPath, strings.NewReader(`{"month":"2024-02","materialized_through":null,"rows":[]}`))
	assertErrorResponse(t, response, http.StatusServiceUnavailable, "service_overloaded", true)
	response = serve(handler, http.MethodGet, "/health", nil)
	if response.Code != http.StatusOK {
		t.Fatalf("health should not use summary capacity")
	}
}

func TestOversizedHTTPBodyReturns413(t *testing.T) {
	response := serve(newHandler(), http.MethodPost, monthlySummaryPath, &repeatReader{remaining: maxRequestBytes + 1})
	assertErrorResponse(t, response, http.StatusRequestEntityTooLarge, "request_too_large", false)
}

func TestCancellationAndRestartDoNotRetainState(t *testing.T) {
	input := request{Month: "2024-02", Rows: []row{{Day: "2024-02-01", Action: "x", StatusFamily: 2, EventCount: "1"}}}
	canceled, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := summarize(canceled, input); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled summary error = %v, want context canceled", err)
	}
	completed, err := summarize(context.Background(), input)
	if err != nil || completed.Totals.EventCount != "1" {
		t.Fatalf("valid request after cancellation: summary=%+v err=%v", completed, err)
	}

	handler := newHandler()
	server := httptest.NewServer(handler)
	postSummary(t, server.URL, `{"month":"2024-02","materialized_through":null,"rows":[{"day":"2024-02-01","action":"x","target_type":"","actor_role":"","status_family":2,"event_count":"1"}]}`, "1")
	postEmptySummary(t, server.URL)
	server.Close()
	server = httptest.NewServer(newHandler())
	defer server.Close()
	postEmptySummary(t, server.URL)
}

func postEmptySummary(t *testing.T, baseURL string) {
	t.Helper()
	postSummary(t, baseURL, `{"month":"2024-02","materialized_through":null,"rows":[]}`, "0")
}

func postSummary(t *testing.T, baseURL, body, expectedCount string) response {
	t.Helper()
	client := &http.Client{Timeout: time.Second}
	httpResponse, err := client.Post(baseURL+monthlySummaryPath, "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer httpResponse.Body.Close()
	var result response
	if err := json.NewDecoder(httpResponse.Body).Decode(&result); err != nil {
		t.Fatal(err)
	}
	if result.Totals.EventCount != expectedCount || result.Daily[0].EventCount != expectedCount {
		t.Fatalf("event count = %s (daily %s), want %s", result.Totals.EventCount, result.Daily[0].EventCount, expectedCount)
	}
	return result
}

func serve(handler http.Handler, method, path string, body io.Reader) *httptest.ResponseRecorder {
	request := httptest.NewRequest(method, path, body)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	return response
}

func assertErrorResponse(t *testing.T, response *httptest.ResponseRecorder, status int, code string, retryable bool) {
	t.Helper()
	if response.Code != status {
		t.Fatalf("status = %d, want %d; body=%s", response.Code, status, response.Body.String())
	}
	var result errorResponse
	if err := json.Unmarshal(response.Body.Bytes(), &result); err != nil {
		t.Fatalf("decode error response: %v", err)
	}
	if result.Code != code || result.Message == "" || result.Retryable != retryable {
		t.Fatalf("error body = %+v", result)
	}
}

func assertJSONEqual(t *testing.T, actual any, expected []byte) {
	t.Helper()
	actualBytes, err := json.Marshal(actual)
	if err != nil {
		t.Fatal(err)
	}
	var actualValue, expectedValue any
	if err := json.Unmarshal(actualBytes, &actualValue); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(expected, &expectedValue); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(actualValue, expectedValue) {
		t.Fatalf("response mismatch\ngot:  %s\nwant: %s", actualBytes, expected)
	}
}

type repeatReader struct {
	remaining int64
}

func (reader *repeatReader) Read(buffer []byte) (int, error) {
	if reader.remaining == 0 {
		return 0, io.EOF
	}
	count := int64(len(buffer))
	if count > reader.remaining {
		count = reader.remaining
	}
	for index := int64(0); index < count; index++ {
		buffer[index] = ' '
	}
	reader.remaining -= count
	return int(count), nil
}
