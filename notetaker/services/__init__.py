"""Clients for the external systems the pipeline talks to: YouTube, the LLM and Notion.

Each client turns provider-specific failures into the domain error hierarchy
(FatalError / RetryableError / RateLimitedError) and goes through the shared rate limiter.
"""
