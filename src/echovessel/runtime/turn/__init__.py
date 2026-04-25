"""Per-turn handling — coordinator, prompt assembly, dispatcher, tracer.

This sub-package contains the code that runs once per user-bot
exchange: building the prompt from memory, calling the LLM, and
ingesting the result. Files land here in commit C1.
"""
