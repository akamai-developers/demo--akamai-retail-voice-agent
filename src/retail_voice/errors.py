class Denied(Exception):
    pass


class Conflict(Exception):
    pass


class ToolArgumentError(Denied):
    """A model tool call with a bad shape. Reported back to the model; never reaches the catalog."""


class ContextLimit(Exception):
    """The current message cannot fit the validated context budget, even with no history."""


class ToolBudgetExhausted(Exception):
    """The model used its decisions or tool calls without producing an answer."""
