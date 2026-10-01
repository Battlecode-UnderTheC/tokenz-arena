"""The HTTP API (DESIGN.md section 6). Submitting is the one write; everything else reads.

Served by `arena serve` (uvicorn) in the same process as the scheduler and judge workers, so
it reads their in-memory state directly.
"""

# TODO(build order 2, 5): FastAPI app with /api/v1/*, /health, /llms.txt
