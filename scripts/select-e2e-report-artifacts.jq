[.[].artifacts[] | select(.name | startswith("playwright-report-"))]
| group_by(.name)
| map(max_by(.created_at))
| map(.id | tostring)
| join(",")
