package main

import (
	"context"
	"encoding/json"
	"os"
	"testing"
)

var (
	benchmarkRequestSink  request
	benchmarkResponseSink response
	benchmarkBytesSink    []byte
)

func benchmarkInput(b *testing.B) ([]byte, request) {
	b.Helper()
	path := os.Getenv("AAP_AUDIT_BENCH_REQUEST")
	if path == "" {
		b.Fatal("AAP_AUDIT_BENCH_REQUEST is required")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		b.Fatal(err)
	}
	input, err := decodeRequest(data)
	if err != nil {
		b.Fatal(err)
	}
	return data, input
}

func BenchmarkDecodeRequest(b *testing.B) {
	data, _ := benchmarkInput(b)
	b.SetBytes(int64(len(data)))
	b.ReportAllocs()
	b.ResetTimer()
	for range b.N {
		input, err := decodeRequest(data)
		if err != nil {
			b.Fatal(err)
		}
		benchmarkRequestSink = input
	}
}

func BenchmarkSummarize(b *testing.B) {
	_, input := benchmarkInput(b)
	b.ReportAllocs()
	b.ResetTimer()
	for range b.N {
		result, err := summarize(context.Background(), input)
		if err != nil {
			b.Fatal(err)
		}
		benchmarkResponseSink = result
	}
}

func BenchmarkEncodeResponse(b *testing.B) {
	_, input := benchmarkInput(b)
	result, err := summarize(context.Background(), input)
	if err != nil {
		b.Fatal(err)
	}
	encoded, err := json.Marshal(result)
	if err != nil {
		b.Fatal(err)
	}
	b.SetBytes(int64(len(encoded)))
	b.ReportAllocs()
	b.ResetTimer()
	for range b.N {
		value, err := json.Marshal(result)
		if err != nil {
			b.Fatal(err)
		}
		benchmarkBytesSink = value
	}
}
