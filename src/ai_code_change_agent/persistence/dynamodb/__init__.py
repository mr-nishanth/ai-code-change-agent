"""DynamoDB adapters (S2116). The only application database; see docs/aws/DYNAMODB-DATA-MODEL.md.

Foundations: `keys` (key builders), `codec` (attribute encoding), `table` (async facade),
`transactions` (`TxBuilder`, `DynamoUnitOfWork`), `errors` (mapping and retry), `clock`."""

from ai_code_change_agent.persistence.dynamodb.clock import Clock, SystemClock
from ai_code_change_agent.persistence.dynamodb.errors import (
    ConcurrentModificationError,
    CorruptItemError,
    DocumentTooLargeError,
    PersistenceError,
    RetryPolicy,
    TransactionTooLargeError,
)
from ai_code_change_agent.persistence.dynamodb.keys import Key
from ai_code_change_agent.persistence.dynamodb.table import Table
from ai_code_change_agent.persistence.dynamodb.transactions import DynamoUnitOfWork, TxBuilder

__all__ = [
    "Clock",
    "ConcurrentModificationError",
    "CorruptItemError",
    "DocumentTooLargeError",
    "DynamoUnitOfWork",
    "Key",
    "PersistenceError",
    "RetryPolicy",
    "SystemClock",
    "Table",
    "TransactionTooLargeError",
    "TxBuilder",
]
