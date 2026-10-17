"""Scriptable project learning workflows."""

import typer

from . import (
    training,
    task_datasets,
    training_datasets,
    training_runs,
    training_recipes,
    training_previews,
    trained_models,
    model_releases,
    learning_policies,
    sample_evaluations,
    training_studies,
    active_learning,
)

app = typer.Typer(help="训练、评估、模型回流与人工主动学习。", no_args_is_help=True)
app.add_typer(training.app, name="training")
app.add_typer(task_datasets.app, name="task-datasets")
app.add_typer(training_datasets.app, name="datasets")
app.add_typer(training_runs.app, name="runs")
app.add_typer(training_recipes.app, name="recipes")
app.add_typer(training_previews.app, name="previews")
app.add_typer(trained_models.app, name="models")
app.add_typer(model_releases.app, name="releases")
app.add_typer(learning_policies.app, name="policies")
app.add_typer(sample_evaluations.app, name="evaluations")
app.add_typer(training_studies.app, name="studies")
app.add_typer(active_learning.app, name="active-learning")
