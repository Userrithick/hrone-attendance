# Design decisions

1. **Indexes.** Unique employee-code and employee/date indexes enforce identity and one record per day. Date/employee and status/date/employee indexes support attendance pages and monthly scans. Employee/punch-in selects the latest eligible punch. Department/code supports employee pages; joined-on/department and department/joined-on support headcount queries. I rejected a late-minutes index: monthly totals and ranks must be computed, so it would not order the leaderboard.

2. **Punch-in race.** Both requests can read the employee and compute the same attendance day. Both attempt insertion. MongoDB's unique compound index accepts one; the other's duplicate-key exception becomes 409. A preliminary existence check is unnecessary and would not provide the guarantee.

3. **Ties.** MongoDB assigns competition ranks before filtering. A limit of two returns ranks 1, 2, 2 when second place is tied. Employee code orders tied rows but does not participate in ranking.

4. **Headcount.** The department pipeline begins with eligible employees. Each employee remains present when its log lookup is empty. Headcount therefore includes zero-log employees; hours use total hours divided by record count.

5. **100x data.** I would introduce incrementally maintained daily department summaries, updated when corrections occur, and measure freshness against query cost. These would reduce repeated attendance lookups in long trend requests.
