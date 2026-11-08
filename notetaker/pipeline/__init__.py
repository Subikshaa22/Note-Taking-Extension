"""The four pipeline stages (spec §3) and the pure logic they are built from.

Each stage handler is idempotent: it first looks for its own output in the job's stored
artifacts and only does the work that is still missing, so a redelivered message (after a
worker crash or a retry) never repeats completed work or duplicates side effects.
"""
