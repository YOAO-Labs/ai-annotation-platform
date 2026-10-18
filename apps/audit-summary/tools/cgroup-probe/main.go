package main

import (
	"encoding/json"
	"os"
	"strings"
)

func main() {
	values := map[string]string{}
	for _, name := range []string{"memory.current", "memory.peak", "memory.max", "memory.events", "memory.swap.current", "memory.swap.max", "cpu.stat", "cpu.max"} {
		data, err := os.ReadFile("/sys/fs/cgroup/" + name)
		if err != nil {
			values[name] = err.Error()
		} else {
			values[name] = strings.TrimSpace(string(data))
		}
	}
	if err := json.NewEncoder(os.Stdout).Encode(values); err != nil {
		os.Exit(1)
	}
}
