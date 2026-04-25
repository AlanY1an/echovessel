"""Proactive runtime delivery — scheduler, queue, delivery, audit.

scheduler runs the tick loop and the event queue.
delivery routes a generated message to the right channel.
audit records every decision for the admin Cost / Trace tabs.
Files land here in commit C4.
"""
