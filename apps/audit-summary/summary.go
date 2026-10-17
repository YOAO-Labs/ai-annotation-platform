package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"regexp"
	"sort"
	"strconv"
	"time"
)

var (
	monthPattern = regexp.MustCompile(`^[0-9]{4}-(0[1-9]|1[0-2])$`)
	datePattern  = regexp.MustCompile(`^[0-9]{4}-[0-9]{2}-[0-9]{2}$`)
	countPattern = regexp.MustCompile(`^(0|[1-9][0-9]{0,37})$`)
)

type request struct {
	Month               string
	MaterializedThrough *string
	Rows                []row
}

type row struct {
	Day          string
	Action       string
	TargetType   string
	ActorRole    string
	StatusFamily int
	EventCount   string
}

type response struct {
	Month               string       `json:"month"`
	Timezone            string       `json:"timezone"`
	MaterializedThrough *string      `json:"materialized_through"`
	Totals              totals       `json:"totals"`
	Daily               []dailyPoint `json:"daily"`
	TopActions          []bucket     `json:"top_actions"`
	TargetTypes         []bucket     `json:"target_types"`
	ActorRoles          []bucket     `json:"actor_roles"`
}

type totals struct {
	EventCount      string `json:"event_count"`
	ErrorCount      string `json:"error_count"`
	ActionKindCount int    `json:"action_kind_count"`
}

type dailyPoint struct {
	Day        string `json:"day"`
	EventCount string `json:"event_count"`
	ErrorCount string `json:"error_count"`
}

type bucket struct {
	Key        string `json:"key"`
	EventCount string `json:"event_count"`
}

func decodeRequest(data []byte) (request, error) {
	fields, err := decodeObject(data, "month", "materialized_through", "rows")
	if err != nil {
		return request{}, err
	}
	month, err := decodeString(fields["month"])
	if err != nil {
		return request{}, fmt.Errorf("month: %w", err)
	}
	if _, _, err := parseMonth(month); err != nil {
		return request{}, err
	}

	materializedRaw := bytes.TrimSpace(fields["materialized_through"])
	var materialized *string
	if !bytes.Equal(materializedRaw, []byte("null")) {
		value, err := decodeString(materializedRaw)
		if err != nil {
			return request{}, fmt.Errorf("materialized_through: %w", err)
		}
		if _, err := parseDate(value); err != nil {
			return request{}, fmt.Errorf("materialized_through: %w", err)
		}
		materialized = &value
	}

	rowsRaw := bytes.TrimSpace(fields["rows"])
	if bytes.Equal(rowsRaw, []byte("null")) {
		return request{}, errors.New("rows must be an array")
	}
	var rowValues []json.RawMessage
	if err := json.Unmarshal(rowsRaw, &rowValues); err != nil || rowValues == nil {
		return request{}, errors.New("rows must be an array")
	}
	rows := make([]row, 0, len(rowValues))
	for index, rowRaw := range rowValues {
		value, err := decodeRow(rowRaw, month)
		if err != nil {
			return request{}, fmt.Errorf("rows[%d]: %w", index, err)
		}
		rows = append(rows, value)
	}
	return request{Month: month, MaterializedThrough: materialized, Rows: rows}, nil
}

func decodeRow(data []byte, month string) (row, error) {
	fields, err := decodeObject(data, "day", "action", "target_type", "actor_role", "status_family", "event_count")
	if err != nil {
		return row{}, err
	}
	var value row
	if value.Day, err = decodeString(fields["day"]); err != nil {
		return row{}, fmt.Errorf("day: %w", err)
	}
	day, err := parseDate(value.Day)
	if err != nil {
		return row{}, fmt.Errorf("day: %w", err)
	}
	if day.Format("2006-01") != month {
		return row{}, errors.New("day must be within month")
	}
	if value.Action, err = decodeString(fields["action"]); err != nil {
		return row{}, fmt.Errorf("action: %w", err)
	}
	if value.TargetType, err = decodeString(fields["target_type"]); err != nil {
		return row{}, fmt.Errorf("target_type: %w", err)
	}
	if value.ActorRole, err = decodeString(fields["actor_role"]); err != nil {
		return row{}, fmt.Errorf("actor_role: %w", err)
	}
	statusRaw := bytes.TrimSpace(fields["status_family"])
	if bytes.Equal(statusRaw, []byte("null")) || json.Unmarshal(statusRaw, &value.StatusFamily) != nil {
		return row{}, errors.New("status_family must be an integer")
	}
	switch value.StatusFamily {
	case 0, 2, 3, 4, 5:
	default:
		return row{}, errors.New("status_family must be one of 0, 2, 3, 4, 5")
	}
	if value.EventCount, err = decodeString(fields["event_count"]); err != nil {
		return row{}, fmt.Errorf("event_count: %w", err)
	}
	if !countPattern.MatchString(value.EventCount) {
		return row{}, errors.New("event_count must be a canonical nonnegative decimal string of at most 38 digits")
	}
	return value, nil
}

func decodeObject(data []byte, required ...string) (map[string]json.RawMessage, error) {
	decoder := json.NewDecoder(bytes.NewReader(data))
	var fields map[string]json.RawMessage
	if err := decoder.Decode(&fields); err != nil {
		return nil, errors.New("must be a JSON object")
	}
	if fields == nil {
		return nil, errors.New("must be a JSON object")
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		return nil, errors.New("must contain exactly one JSON value")
	}
	allowed := make(map[string]struct{}, len(required))
	for _, name := range required {
		allowed[name] = struct{}{}
		if _, ok := fields[name]; !ok {
			return nil, fmt.Errorf("missing required field %q", name)
		}
	}
	for name := range fields {
		if _, ok := allowed[name]; !ok {
			return nil, fmt.Errorf("unknown field %q", name)
		}
	}
	return fields, nil
}

func decodeString(data []byte) (string, error) {
	var value string
	if bytes.Equal(bytes.TrimSpace(data), []byte("null")) || json.Unmarshal(data, &value) != nil {
		return "", errors.New("must be a string")
	}
	return value, nil
}

func parseMonth(value string) (int, time.Month, error) {
	if !monthPattern.MatchString(value) {
		return 0, 0, errors.New("month must use YYYY-MM format")
	}
	year, _ := strconv.Atoi(value[:4])
	monthNumber, _ := strconv.Atoi(value[5:])
	if year == 0 {
		return 0, 0, errors.New("month year must be between 0001 and 9999")
	}
	return year, time.Month(monthNumber), nil
}

func parseDate(value string) (time.Time, error) {
	if !datePattern.MatchString(value) {
		return time.Time{}, errors.New("must use YYYY-MM-DD format")
	}
	day, err := time.Parse("2006-01-02", value)
	if err != nil || day.Year() == 0 || day.Format("2006-01-02") != value {
		return time.Time{}, errors.New("must be a valid calendar date")
	}
	return day, nil
}

func summarize(ctx context.Context, input request) (response, error) {
	if err := ctx.Err(); err != nil {
		return response{}, err
	}
	year, month, err := parseMonth(input.Month)
	if err != nil {
		return response{}, err
	}
	firstDay := time.Date(year, month, 1, 0, 0, 0, 0, time.UTC)
	daysInMonth := time.Date(year, month+1, 0, 0, 0, 0, 0, time.UTC).Day()
	dailyCounts := make([]struct{ events, errors big.Int }, daysInMonth)
	actions := make(map[string]*big.Int)
	targetTypes := make(map[string]*big.Int)
	actorRoles := make(map[string]*big.Int)
	var totalEvents, totalErrors big.Int

	for _, item := range input.Rows {
		if err := ctx.Err(); err != nil {
			return response{}, err
		}
		day, err := parseDate(item.Day)
		if err != nil || day.Format("2006-01") != input.Month {
			return response{}, errors.New("row day must be a valid date within month")
		}
		count, ok := new(big.Int).SetString(item.EventCount, 10)
		if !ok || count.Sign() < 0 || !countPattern.MatchString(item.EventCount) {
			return response{}, errors.New("row event_count must be a canonical nonnegative decimal string of at most 38 digits")
		}
		dayIndex := day.Day() - 1
		dailyCounts[dayIndex].events.Add(&dailyCounts[dayIndex].events, count)
		totalEvents.Add(&totalEvents, count)
		if item.StatusFamily == 4 || item.StatusFamily == 5 {
			dailyCounts[dayIndex].errors.Add(&dailyCounts[dayIndex].errors, count)
			totalErrors.Add(&totalErrors, count)
		}
		addCount(actions, item.Action, count)
		addCount(targetTypes, item.TargetType, count)
		addCount(actorRoles, item.ActorRole, count)
	}

	daily := make([]dailyPoint, daysInMonth)
	for index, counts := range dailyCounts {
		daily[index] = dailyPoint{
			Day:        firstDay.AddDate(0, 0, index).Format("2006-01-02"),
			EventCount: counts.events.String(),
			ErrorCount: counts.errors.String(),
		}
	}
	return response{
		Month:               input.Month,
		Timezone:            "UTC",
		MaterializedThrough: input.MaterializedThrough,
		Totals: totals{
			EventCount:      totalEvents.String(),
			ErrorCount:      totalErrors.String(),
			ActionKindCount: len(actions),
		},
		Daily:       daily,
		TopActions:  rankBuckets(actions, 10),
		TargetTypes: rankBuckets(targetTypes, 0),
		ActorRoles:  rankBuckets(actorRoles, 0),
	}, nil
}

func addCount(values map[string]*big.Int, key string, count *big.Int) {
	if values[key] == nil {
		values[key] = new(big.Int)
	}
	values[key].Add(values[key], count)
}

func rankBuckets(values map[string]*big.Int, limit int) []bucket {
	type entry struct {
		key   string
		count *big.Int
	}
	entries := make([]entry, 0, len(values))
	for key, count := range values {
		entries = append(entries, entry{key: key, count: count})
	}
	sort.Slice(entries, func(i, j int) bool {
		if comparison := entries[i].count.Cmp(entries[j].count); comparison != 0 {
			return comparison > 0
		}
		return entries[i].key < entries[j].key
	})
	if limit > 0 && len(entries) > limit {
		entries = entries[:limit]
	}
	result := make([]bucket, len(entries))
	for index, value := range entries {
		result[index] = bucket{Key: value.key, EventCount: value.count.String()}
	}
	return result
}
