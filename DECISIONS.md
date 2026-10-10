# DECISIONS.md

## 1. Indexes

I created five indexes at startup using `create_index` calls that are safe to repeat (idempotent).

- **`employees.emp_code` (unique)** — enforces uniqueness at the database level and powers O(1) employee lookups by `emp_code`.
- **`(attendance_logs.emp_code, date)` (unique compound)** — enforces the one-record-per-day invariant atomically and covers employee-filtered attendance queries.
- **`(attendance_logs.date DESC, emp_code ASC)`** — backs the default sort on `GET /attendance` directly so MongoDB can satisfy the query from the index without a separate sort step.
- **`(attendance_logs.emp_code, punch_in ASC)`** — speeds up the punch-out lookup that finds the most recent open record for an employee (`punch_in <= po_utc, punch_out = null`).
- **`employees.department`** — accelerates department-filter queries in the analytics endpoints.

I considered a text index on `employees.name` for name-based search, but no endpoint searches by name, so I rejected it.

## 2. Punch-in race

Both requests reach the handler simultaneously and both pass the employee-exists check. Both then call `insert_one` with a document that has the same `(emp_code, date)` key. MongoDB processes the two inserts sequentially at the collection level. The first insert succeeds. The second insert tries to insert a document that would violate the unique compound index on `(emp_code, date)` and immediately raises `DuplicateKeyError`. The handler catches it and returns 409. There is no check-then-insert window because the uniqueness guarantee is enforced by the database engine, not by application logic.

## 3. Ties

When two employees share the same `total_late_minutes` at the leaderboard cutoff, I use standard competition ranking (also called 1224 ranking). Both employees receive the same rank, and both are included in the response as long as that rank is ≤ `limit`. If employees A, B, C, D have 90, 60, 60, and 30 minutes respectively and `limit` is 3, then A is rank 1, B and C are both rank 2, and D is rank 4. Because rank 2 ≤ limit 3, both B and C appear. D is excluded because rank 4 > limit 3.

## 4. Headcount

The department summary pipeline starts from the `employees` collection (filtered to `joined_on <= month_end`). It uses `$lookup` with `preserveNullAndEmptyArrays: true` followed by `$unwind` with the same flag. This keeps an employee document in the pipeline even when no attendance logs match. The subsequent `$group` uses `$addToSet` on `emp_code`, so each employee is counted exactly once regardless of how many (or zero) logs they contributed. The final `$project` uses `$size` of the set to get the headcount.

## 5. One thing I would change at 100× scale

I would add a pre-aggregated monthly summary collection (`attendance_summary_monthly`) written by a background job triggered after each punch-out or correction. The analytics endpoints would then read from this small summary collection instead of scanning and grouping the full `attendance_logs` collection on every request. This trades a small write-time cost for much faster read performance. On the write side, I would shard `attendance_logs` on `emp_code` so that punch-in and punch-out operations for the same employee always land on the same shard, keeping each write to a single shard with no cross-shard coordination.
