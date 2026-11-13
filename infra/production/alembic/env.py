"""Dedicated operator path; local Alembic environment remains unchanged."""
import re

from alembic import context
from sqlalchemy import Connection, text

config = context.config
connection = config.attributes.get('production_connection')
nonce = config.attributes.get('production_nonce', '')
if not isinstance(connection, Connection) or not re.fullmatch(r'[a-f0-9]{24}', nonce):
    raise RuntimeError('Explicit production installer connection required.')
if (connection.engine.url.host, connection.engine.url.database, connection.engine.url.username) != ('db', 'kpdesk', 'kpdesk'):
    raise RuntimeError('Production installer endpoint mismatch.')
if connection.scalar(text("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()")) != 'kpdesk:production:' + nonce:
    raise RuntimeError('Production instance marker mismatch.')
context.configure(connection=connection, target_metadata=None, transactional_ddl=True)
with context.begin_transaction():
    context.run_migrations()
