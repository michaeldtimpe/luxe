"""Pure-function utilities shared across the extended-benchmark suite.

Everything in this package is offline-testable: no Backend imports, no
network, no model loading. Run-orchestration code lives in each benchmark's
run.py module; scoring/parsing/dataset utilities live here.
"""
