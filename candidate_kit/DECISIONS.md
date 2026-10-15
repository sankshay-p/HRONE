# DECISIONS.md

## Core Architecture Decisions

### 1. Indexes
- **`employees`**: Unique `(emp_code)` enforces employee identity; `(department, emp_code)` serves filtered pagination; `(department, joined_on)` supports headcount filtering.
- **`attendance_logs`**: Unique `(emp_code, date)` guarantees at most one record per day race-safely; compound `(date: -1, emp_code: 1)` serves attendance listing and range queries without in-memory sorting; `(emp_code, punch_in: -1)` backs punch-out lookups; `(date, late_minutes)` backs late leaderboard filtering; `(emp_code, date)` prefix covers weekly date-range queries.
- **Rejected Index**: A standalone index on `attendance_logs.emp_code` was rejected because the leftmost prefix of `(emp_code, date)` already satisfies single-employee queries without redundant write overhead and storage bloat.

### 2. Punch-in Race Guarantee
Both concurrent requests validate the employee, determine the IST attendance date, and attempt `insert_one`. MongoDB's unique index on `(emp_code, date)` guarantees that only one write commits. The winner returns `201 Created`. The second request raises `DuplicateKeyError`, caught and converted into `409 Conflict`. Zero check-then-insert race window exists.

### 3. Ties in Ranking
The leaderboard uses MongoDB's `$rank` window operator (standard competition ranking: 1, 2, 2, 4) and filters `rank <= limit` *after* ranking. Both tied employees receive the same cutoff rank and are both included, ordered deterministically by `emp_code` ascending.

### 4. Headcount Preservation (0-log employees)
The department aggregation originates from the `employees` collection filtered by `joined_on <= end_of_month` and performs a `$lookup` to `attendance_logs`. Using `$unwind` with `preserveNullAndEmptyArrays: true` preserves employees with empty logs. Grouping sums distinct employees (`$sum: 1`), ensuring 100% accurate headcount.

### 5. Scale to 100x Data
At 100x scale (~10M records), computing cross-collection aggregations on every request would saturate database CPU/RAM. I would implement incremental daily and monthly rollup collections updated via change streams or background workers, turning analytics queries into O(1) document lookups.

---

## New Analytics & Power BI Decisions

### 6. Weekly Timing & Work Hours (`/analytics/employees/{emp_code}/weekly`)
- **Calendar-Gap Densification**: Rather than returning only days with punches, the endpoint constructs all 7 days (Monday–Sunday) from `start_date`. Unrecorded weekdays are tagged `NOT_RECORDED`, and weekends are tagged `OFF`.
- **Amount of Time Formatting**: Implemented `format_hours_to_amount_of_time(hours)` converting raw floating-point hours into human-readable `"Xh YYm"` strings (e.g. `9.12` → `"9h 07m"`). Days without punches or employees with zero attendance format cleanly as `"—"`.
- **Shift & Target Metrics**: Dynamically parses the employee's configured shift (e.g., `09:30` – `18:30` = 9.0h) to compute scheduled weekly hours (45h), on-time percentage (R2 grace rule of 10 minutes), daily average, and weekly completion rate.

### 7. Peer Statistics & Benchmark Comparison (`/analytics/employees/{emp_code}/comparison`)
- **Single-Roundtrip Multi-Level Benchmark**: Combines employee metrics, department peer rankings, and company-wide distributions in one atomic response.
- **Competition Ranking**: Uses standard competition ranking across both department and company levels.
- **Percentile Spectrum**: Derives company-wide five-number summary (Min, Q25, Median, Q75, Max) and exact percentile rank using sample interpolation, enabling distribution visualization.
- **Zero-Log Peer Integrity**: Queries all department employees and overlays aggregated hours, ensuring colleagues with zero logs in the month are represented in peer tables and headcounts.

### 8. Power BI Analytics Dashboard (`/` & `/dashboard`)
- **Design Language**: Follows Microsoft Power BI's exact visual system (charcoal ribbon header, bottom-aligned page tabs, slicer filter bar, canvas cards, and official palette: `#118dff`, `#12b5cb`, `#e66c37`, `#37b54a`, `#e04856`, `#8c52ff`).
- **Interactive Pages**:
  1. *Overview*: KPI cards with donut ring, daily work hours bar chart with shift target line, Gantt shift timeline vs actual punch in/out, weekly matrix table.
  2. *Weekly Timing*: Regular vs overtime stacked area trend, punctuality trend with 10-min grace line, semi-circle time utilization gauge.
  3. *Peer Comparison*: Grouped comparative bar chart (You vs Dept vs Company), percentile distribution spectrum, department peer leaderboard table.
  4. *Departments*: Horizontal bar charts for average hours and late minutes, department summary matrix.
