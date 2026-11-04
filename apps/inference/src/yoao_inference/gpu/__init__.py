"""GPU dispatch and physical-residency authority.

The package root is intentionally lazy: importing the public contracts does not load
Redis, signing keys, database adapters, or backend transports.
"""
