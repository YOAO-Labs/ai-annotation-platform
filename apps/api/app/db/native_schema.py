"""Keep native-owned state out of ORM migration autogeneration."""

from app.assembly.builders import (
    collect_native_managed_relations,
    collect_native_managed_tables,
)

NATIVE_MANAGED_TABLES = collect_native_managed_tables()
NATIVE_MANAGED_RELATIONS = collect_native_managed_relations()


def include_orm_schema_object(object_, name, type_, reflected, compare_to):
    """Native SQL migrations remain the sole schema owner for these relations."""
    return not (
        type_ == "table"
        and (getattr(object_, "schema", None) or "public", name)
        in NATIVE_MANAGED_RELATIONS
    )
