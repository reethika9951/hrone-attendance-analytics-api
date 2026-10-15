# Employee Attendance & Analytics API

A Python FastAPI and MongoDB API for employee records, attendance punch-in/punch-out, attendance correction, and analytics. This repository was prepared for the HROne Software Engineer Trainee assignment.

## Requirements

- Python 3.11 or newer
- MongoDB running locally, or a MongoDB Atlas connection
- Git

## Run on Windows

Open Command Prompt in the repository root (the folder containing `requirements.txt`) and run:

```bat
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Open the interactive API documentation at `http://127.0.0.1:8000/docs`.

The app defaults to `mongodb://localhost:27017` and database `attendance_db`. To use a different database, set `MONGO_URI` and `MONGO_DB` environment variables before starting the app. For local development, a `.env` file can be used because `python-dotenv` is included. **Never commit `.env`, credentials, or secrets.**

## Main endpoints

- `GET /health` — checks MongoDB connectivity.
- `POST /employees`, `GET /employees` — create and list employees.
- `POST /attendance/punch-in`, `POST /attendance/punch-out` — record attendance.
- `GET /attendance` — list attendance records.
- `PATCH /attendance/{emp_code}/{att_date}` — correct an attendance record.
- `/analytics/...` — monthly, department, leaderboard, and trend analytics.
- `GET /admin/explain/{endpoint}` — inspect query plans for supported endpoints.

Use `/docs` to see the exact request fields and required query parameters for each endpoint.

## Data and implementation notes

- API timestamps are integer epoch milliseconds; MongoDB stores datetimes in UTC.
- The application creates indexes on startup, including unique employee-code and employee/date attendance indexes.
- Analytics endpoints use MongoDB aggregation pipelines.
- Read `DECISIONS.md` for design choices and `REVIEW.md` for the testing status 

## Testing status and limitations

Manual smoke tests were performed locally using the Swagger UI and a local MongoDB instance. The following were observed to work during those tests: health check (`200`), employee creation (`201`), employee listing (`200`), punch-in (`201`), punch-out (`200`), attendance listing (`200`), monthly analytics (`200`), department summary (`200`), leaderboard (`200`), department trend (`200`), attendance correction (`200`), and the `attendance_list` explain request (`200`). The explain result for `attendance_list` showed an `IXSCAN` using `attendance_date_employee` and did not show `COLLSCAN` in the winning plan.

These are smoke-test results, not a complete automated acceptance test. All analytics calculations, all explain variants, concurrent requests, and performance with 100,000 records have **not** been fully verified. During manual testing, an attendance record had a very short punch-in/punch-out interval (6 minutes 50 seconds) and returned `work_hours: 0.11`; other derived values for that test record need further validation. Do not treat the current smoke tests as proof that every assignment requirement is satisfied.


