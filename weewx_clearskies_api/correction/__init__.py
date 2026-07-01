"""Forecast correction engine package (ADR-079).

Collects forecast-observation pairs and trains a Random Forest model to
correct systematic forecast temperature bias.  Provider-agnostic.

Sub-modules:
    db      — Separate SQLite engine, schema creation, CRUD functions.
    models  — Pydantic response/request models for the admin endpoints.
"""
