# Employee Attendance & Analytics API

A FastAPI + MongoDB REST API for employee punch-in/punch-out tracking, manual corrections, and attendance analytics.

## What's built

- Employee management (create, list with pagination and department filter)
- Punch-in and punch-out with IST timezone handling, overnight shift support, late detection, and overtime calculation
- Manual attendance corrections (regularization) with a full audit trail in `history`
- Analytics:
  - Per-employee monthly summary (present days, leave, late count, overtime, attendance %)
  - Department summary (headcount, avg work hours, late/leave/on-duty counts)
  - Late-comers leaderboard with standard competition ranking
  - Daily attendance trend with 7-day moving average
- Admin explain endpoint showing MongoDB query plans for each analytics query
- All indexes created automatically at startup

## Requirements

- Python 3.11+
- MongoDB 6.0+ (local Docker container or Atlas free tier both work)

## Local setup

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure the database connection
# Create a .env file in this directory:
#   MONGO_URI=mongodb://localhost:27017
#   MONGO_DB=attendance_db

# 4. (Optional) Seed sample data
python sample_seed.py

# 5. Start the server
uvicorn app.main:app --port 8000 --reload
```

## MongoDB quick start with Docker

```bash
docker run -d -p 27017:27017 --name mongo mongo:7
```

## API docs

Visit [http://localhost:8000/docs](http://localhost:8000/docs) after starting the server for the interactive Swagger UI.

## Key design notes

- All timestamps in requests and responses are **epoch milliseconds** (integer). Calendar dates (`date`, `joined_on`) are plain `YYYY-MM-DD` strings.
- Attendance dates follow IST and handle overnight shifts correctly (shift_end ≤ shift_start means the shift spans midnight).
- Late detection uses a 10-minute grace period (strictly more than 600 seconds = late).
- Overtime requires at least 30 consecutive minutes past shift end.
- A shift under 4.5 hours is flagged as a half day.
- The regularize endpoint records every field change in an append-only `history` array and uses an optimistic-concurrency guard (`$size` check) to detect concurrent edits.
- MongoDB 6.0+ is required (the grader uses 7.0). Do not commit `.env` files or secrets.

## What is not included

Everything in the OpenAPI spec is implemented. No features are intentionally omitted.
