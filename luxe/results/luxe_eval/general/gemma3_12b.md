# General-agent eval — `gemma3:12b`

## factual (3280 ms, 1 words)

**Prompt:** What is the capital of France?

Paris.

## conceptual (3882 ms, 61 words)

**Prompt:** Explain recursion in one short paragraph.

Recursion is a programming technique where a function calls itself within its own definition to solve a problem by breaking it down into smaller, self-similar subproblems. Each recursive call works on a smaller piece of the original problem until a base case is reached, which stops the recursion and allows the results to be combined back up to the initial call.

## how-to (3505 ms, 29 words)

**Prompt:** How do I rename a local git branch?

1. **Rename locally:** `git branch -m old-branch-name new-branch-name`
2. **Push the renamed branch (if needed):** `git push -u origin new-branch-name` (This replaces the remote branch with the new name.)

## tradeoff (3094 ms, 35 words)

**Prompt:** When should I pick a relational database over a document database?

Choose a relational database when:

*   Data has well-defined relationships.
*   ACID transactions are critical.
*   Complex joins and queries are frequently needed.

Document databases are better when data is semi-structured and flexibility is paramount.

## definition (4437 ms, 52 words)

**Prompt:** What is the difference between concurrency and parallelism?

Concurrency is about *dealing* with multiple tasks at once. Parallelism is about *doing* multiple tasks at once.

*   **Concurrency:** Multiple tasks progress, but not necessarily simultaneously. Think of a chef juggling multiple dishes—they switch between them.
*   **Parallelism:** Multiple tasks run simultaneously. Think of multiple chefs each working on a different dish.

## list-format (5526 ms, 70 words)

**Prompt:** Name 3 books similar to The Lord of the Rings. One sentence each.

*   *The Chronicles of Narnia* by C.S. Lewis: A classic fantasy series featuring magical creatures and a battle between good and evil.
*   *The Wheel of Time* by Robert Jordan: An epic fantasy with a vast world, intricate magic system, and a chosen one narrative.
*   *A Song of Ice and Fire* by George R.R. Martin: A gritty, politically complex fantasy series with morally grey characters and a sprawling scope.

## multi-fact (1146 ms, 9 words)

**Prompt:** What year was Python created, and by whom?

Python was created in 1991 by Guido van Rossum.

## redirect (1657 ms, 21 words)

**Prompt:** Write me a full 5-chapter novel about dragons.

That request requires creative writing capabilities beyond my current scope; please rephrase your request to direct it to a specialist agent.
