# Employee Attendance & Analytics API

A FastAPI and MongoDB backend service implementing employee attendance tracking, regularization, and analytics under strict business rules (R1 - R10) and concurrency guarantees.

## Setup & Running

### Requirements
- Python 3.11+
- MongoDB 6.0+ (Tested on MongoDB 7.0)

### Quickstart
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Start the application (reads MONGO_URI and MONGO_DB from environment or .env)
uvicorn app.main:app --port 8000
```

All indexes are created idempotently at application startup via FastAPI lifespan.

### Seed Sample Data (Optional)
```bash
python sample_seed.py
```

### Running Tests
```bash
PYTHONPATH=. pytest tests/
```

## Implementation Highlights

- **All Application Code in `app/main.py`**: Adheres strictly to the single-file packaging requirement.
- **Race-Safe Concurrency**:
  - `POST /attendance/punch-in`: Enforced by MongoDB unique compound index `(emp_code, date)` and `DuplicateKeyError` handling (yielding exactly one 201 and 409 for conflicts).
  - `POST /attendance/punch-out`: Atomic document modification via `find_one_and_update` on `{"punch_out": None}`.
  - `PATCH /attendance/{emp_code}/{date}`: Optimistic concurrency control against history size ensuring zero lost updates.
- **Pure MongoDB Aggregations**:
  - `GET /analytics/departments/summary`: Preserves 0-log employees using left outer join (`$lookup` + `$unwind` with `preserveNullAndEmptyArrays`). Computes exact record-level mean work hours.
  - `GET /analytics/leaderboard/late`: Native standard competition ranking (1, 2, 2, 4) using `$setWindowFields` and `$rank`, applying limits post-ranking.
  - `GET /analytics/departments/{department}/trend`: Calendar-gap densification via native `$densify` and rolling 7-day window moving average via `$setWindowFields`.
- **Explain Verification**:
  - `GET /admin/explain/{endpoint}`: Backed by targeted compound indexes, guaranteeing `IXSCAN` plans without `COLLSCAN`.
