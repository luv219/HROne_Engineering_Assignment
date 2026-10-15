# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?

   Unique `emp_code` and unique `(emp_code, date)` make MongoDB reject duplicates. `(emp_code, date)` also serves the monthly summary and the per-employee lookups in summary and trend. `(date desc, emp_code)` matches the attendance-list sort, `(date, late_minutes)` the leaderboard, `(emp_code, punch_in)` punch-out, and the `department`/`joined_on` indexes headcount and the employee list. A standalone `department` index was rejected: the compound indexes cover that prefix. `(date, status)` went unused in the 100k benchmark.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.

   Both find the employee, compute the same IST date and call `insert_one`. The unique index admits one; the other gets `DuplicateKeyError`, returned as 409. A find-then-insert would let both pass the check.

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?

   Both. `$rank` gives competition ranks (1, 2, 2, 4) and the filter `rank <= limit` runs after ranking, so ties at the cutoff can exceed `limit`. Cutting by array position would drop one arbitrarily.

4. **Headcount.** How does your department summary make sure employees with zero logs are counted?

   It starts from `employees` with `joined_on` ≤ month end, looks up each person's logs, and `$unwind` keeps empty results, so people without logs still count. Averages are over records.

5. **One thing you would change** if this had to serve 100x the data.

   Keep monthly per-employee and daily per-department totals, updated on every punch and PATCH, so analytics read hundreds of rows, not ~800k logs a month. Cost: every write path must keep them consistent.
