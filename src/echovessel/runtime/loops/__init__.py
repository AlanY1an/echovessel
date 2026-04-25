"""Background asyncio tick loops — consolidate worker, idle scanner.

These are not job-queue consumers; they wake on a timer, scan for
work, and sleep. Files land here in commit C2.
"""
