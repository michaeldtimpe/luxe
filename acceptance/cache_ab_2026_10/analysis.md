| arm | runs | pass | score | avg wall s | avg tokens | cached % | prefill s (sum) | decode s (sum) | cont. steps warm/partial (of which compaction)/cold | re-prefilled tokens (compaction) | compaction fires | reasoning chars |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| base | 30 | 30/30 | 120/150 | 47.4 | 136,807 | 86.5% | 181.9 | 1217.7 | 321/57 (41)/0 | 452,009 (438,737) | 66 | 114,649 |
| R | 30 | 29/30 | 117/150 | 34.3 | 124,823 | 85.7% | 177.5 | 832.3 | 315/22 (22)/0 | 308,209 (308,209) | 33 | 119,756 |
| C | 30 | 30/30 | 120/150 | 43.5 | 135,118 | 89.5% | 153.2 | 1133.7 | 330/39 (23)/0 | 351,994 (338,522) | 27 | 118,599 |
| RC | 30 | 30/30 | 120/150 | 35.7 | 126,662 | 88.5% | 149.9 | 898.8 | 312/24 (21)/0 | 316,707 (314,475) | 27 | 121,364 |

Per fixture (passes/reps [scores]  avg wall):
```
fixture                                                       base                         R                         C                        RC
isomer-document-quickstart                       3/3 [4, 4, 4] 20s         3/3 [4, 4, 4] 20s         3/3 [4, 4, 4] 20s         3/3 [4, 4, 4] 20s
isomer-implement-healthcheck                     3/3 [4, 4, 4] 28s         3/3 [4, 4, 4] 18s         3/3 [4, 4, 4] 28s         3/3 [4, 4, 4] 18s
lpe-rope-calc-document-typing                    3/3 [4, 4, 4] 30s         3/3 [4, 4, 4] 28s         3/3 [4, 4, 4] 29s         3/3 [4, 4, 4] 29s
lpe-rope-calc-implement-strict-flag             3/3 [4, 4, 4] 155s         3/3 [4, 4, 4] 82s        3/3 [4, 4, 4] 140s         3/3 [4, 4, 4] 90s
neon-rain-document-modules                       3/3 [4, 4, 4] 24s         3/3 [4, 4, 4] 24s         3/3 [4, 4, 4] 24s         3/3 [4, 4, 4] 24s
neon-rain-implement-reset-shortcut               3/3 [4, 4, 4] 54s         3/3 [4, 4, 4] 56s         3/3 [4, 4, 4] 54s         3/3 [4, 4, 4] 51s
nothing-ever-happens-document-config             3/3 [4, 4, 4] 93s         2/3 [4, 1, 4] 48s         3/3 [4, 4, 4] 67s         3/3 [4, 4, 4] 58s
nothing-ever-happens-manage-deps-audit           3/3 [4, 4, 4] 40s         3/3 [4, 4, 4] 37s         3/3 [4, 4, 4] 41s         3/3 [4, 4, 4] 36s
the-game-document-architecture                   3/3 [4, 4, 4] 19s         3/3 [4, 4, 4] 10s         3/3 [4, 4, 4] 19s         3/3 [4, 4, 4] 11s
the-game-implement-shuffle-shortcut              3/3 [4, 4, 4] 11s         3/3 [4, 4, 4] 21s         3/3 [4, 4, 4] 11s         3/3 [4, 4, 4] 21s
```
