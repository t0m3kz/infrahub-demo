"""The states of an Infrahub task that the generators and the kick runner both read."""

from infrahub_sdk.task.models import TaskState

IN_FLIGHT_STATES = [TaskState.PENDING, TaskState.RUNNING, TaskState.SCHEDULED]
FAILURE_STATES = [TaskState.FAILED, TaskState.CRASHED, TaskState.CANCELLED]
