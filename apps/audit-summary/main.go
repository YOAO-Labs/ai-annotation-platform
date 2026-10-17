package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	listenAddress := flag.String("listen", "127.0.0.1:8091", "TCP address to listen on")
	healthcheck := flag.Bool("healthcheck", false, "check localhost:8091/health and exit")
	flag.Parse()

	if *healthcheck {
		if err := checkHealth(); err != nil {
			log.Fatal(err)
		}
		return
	}
	shutdownContext, stopSignals := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stopSignals()

	listener, err := net.Listen("tcp", *listenAddress)
	if err != nil {
		log.Fatal(err)
	}
	server := &http.Server{
		Handler:           newHandler(),
		ReadTimeout:       30 * time.Second,
		ReadHeaderTimeout: 5 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    1 << 20,
	}

	serveErr := make(chan error, 1)
	go func() { serveErr <- server.Serve(listener) }()
	if err := json.NewEncoder(os.Stdout).Encode(map[string]string{
		"event":   "ready",
		"address": listener.Addr().String(),
		"service": "audit-summary",
	}); err != nil {
		log.Printf("write readiness record: %v", err)
	}

	select {
	case <-shutdownContext.Done():
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := server.Shutdown(ctx); err != nil {
			log.Printf("graceful shutdown: %v", err)
			_ = server.Close()
		}
		if err := <-serveErr; err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Printf("serve: %v", err)
		}
	case err := <-serveErr:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatal(err)
		}
	}
}

func checkHealth() error {
	client := &http.Client{Timeout: 2 * time.Second}
	response, err := client.Get("http://localhost:8091/health")
	if err != nil {
		return fmt.Errorf("health check: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("health check: unexpected status %s", response.Status)
	}
	var health healthResponse
	if err := json.NewDecoder(response.Body).Decode(&health); err != nil {
		return fmt.Errorf("health check response: %w", err)
	}
	if health.Service != "audit-summary" || health.Protocol != 1 {
		return errors.New("health check: unexpected service protocol")
	}
	return nil
}
