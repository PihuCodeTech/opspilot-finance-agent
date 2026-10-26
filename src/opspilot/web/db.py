"""One explicit transaction per request step, opened by the route itself.

Routes call `transaction(request)` around their service call, so the commit
happens before the response is built and a failure rolls everything back.
Tests swap the factory for a savepoint on a connection they roll back.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager

from sqlalchemy import Connection, Engine
from starlette.requests import Request

TransactionFactory = Callable[[], AbstractContextManager[Connection]]


def engine_transactions(engine: Engine) -> TransactionFactory:
    return engine.begin


def transaction(request: Request) -> AbstractContextManager[Connection]:
    factory: TransactionFactory = request.app.state.transactions
    return factory()
