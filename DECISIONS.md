# Implementation decisions

1. **Indexes:** I create a unique index on employee code and a unique compound index on attendance `(emp_code, date)` to enforce identity and prevent duplicate punch-ins. I also index department/code, department/join date, attendance date/code, employee/date/status, and status/date for the list and analytics filters. I avoided adding an index for every possible field combination because each index slows writes and consumes storage.
2. **Punch-in race:** Both requests may reach the insert, but MongoDB's unique compound index allows only one record for an employee/date. The successful insert returns 201; the other raises `DuplicateKeyError`, which the API converts to 409.
3. **Ties:** The leaderboard uses MongoDB `$rank`, then filters on `rank <= limit`; ties at the cutoff are all returned. The result is sorted by total late minutes and employee code for stable display.
4. **Headcount:** The department summary starts with employees joined by month-end and looks up logs, preserving employees without logs.
5. **100x scale:** I would review query plans with production-like data, add workload-specific compound indexes, and consider pre-aggregated daily summaries for expensive dashboards.
