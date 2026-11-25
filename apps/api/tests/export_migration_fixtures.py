from sqlalchemy import Table, text
from sqlalchemy.orm import registry

from tests.migration_helpers import bind, load


def create_export_artifacts(connection):
    connection.execute(text("CREATE TABLE projects (id uuid PRIMARY KEY)"))
    for filename in ("0081_export_artifacts.py", "0211_export_artifact_formats.py"):
        bind(load(filename), connection).upgrade()


def export_output_model(connection, schema):
    mappings = registry()

    @mappings.mapped
    class ExportOutput:
        __table__ = Table(
            "export_outputs", mappings.metadata, schema=schema, autoload_with=connection
        )

    return ExportOutput
