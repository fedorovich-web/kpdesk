"""Fail-closed pytest boundaries. Never imported by the application runtime."""
from __future__ import annotations

import os
import re
import socket
import threading
from collections.abc import Callable
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import tuple_row
from sqlalchemy import event
from sqlalchemy.engine import Engine

_NAME = r"kpdesk_accounts_http_[0-9a-f]{12}"
_CREATE = re.compile(rf'CREATE\s+DATABASE\s+"({_NAME})"\s+TEMPLATE\s+template0\s+OWNER\s+kpdesk', re.IGNORECASE)
_COMMENT = re.compile(rf"COMMENT\s+ON\s+DATABASE\s+\"({_NAME})\"\s+IS\s+'(kpdesk:accounts-http:[0-9a-f]{{12}})'", re.IGNORECASE)
_DROP = re.compile(rf'DROP\s+DATABASE\s+"({_NAME})"\s+WITH\s*\(\s*FORCE\s*\)', re.IGNORECASE)
_SAVEPOINT_ROLLBACK = re.compile(r"ROLLBACK\s+TO\s+SAVEPOINT\s+sa_savepoint_[0-9]+", re.IGNORECASE)
# This profile needs schema/data SQL only, never cluster-wide administration.
_CLUSTER_SQL = re.compile(
    r"\b(?:CREATE|ALTER|DROP)\s+(?:ROLE|USER|GROUP|DATABASE|TABLESPACE|SYSTEM)\b"
    r"|\b(?:GRANT|REVOKE)\b|\bpg_(?:terminate|cancel)_backend\s*\(", re.IGNORECASE,
)
_IDENTITY = """SELECT current_database(), current_user, pg_get_userbyid(datdba),
    shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = current_database()"""
_TARGET = """SELECT current_database(), current_user, pg_get_userbyid(datdba),
    shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s"""
_ADMIN_READS = frozenset({
    "selectcurrent_database(),current_user", "selectpg_catalog.version()", "selectcurrent_schema()",
    "showtransactionisolationlevel", "showstandard_conforming_strings",
    ("selectcurrent_database(),current_user,pg_get_userbyid(datdba),"
     "shobj_description(oid,'pg_database')frompg_databasewheredatname=%(name)s"),
    ("selecttypnameasname,oid,typarrayasarray_oid,oid::regtype::textasregtype,"
     "typdelimasdelimiterfrompg_typetwheret.oid=to_regtype(%(name)s)orderbyt.oid"),
})


class SafetyViolation(RuntimeError):
    """The requested test action is outside the reviewed local test boundary."""


class SessionSafety:
    def __init__(self, *, allow_postgres: bool, connector: Callable[..., Any] | None = None) -> None:
        self.allow_postgres = allow_postgres
        self.owned: set[str] = set()
        self.marked: set[str] = set()
        self._connector = connector if connector is not None else psycopg.connect
        self._execute: Callable[..., Any] = psycopg.Cursor.execute
        self._executemany: Callable[..., Any] = psycopg.Cursor.executemany
        self._metadata_state = threading.local()
        self._stack = ExitStack()
        self._started = False

    @staticmethod
    def marker(name: str) -> str:
        if re.fullmatch(_NAME, name) is None:
            raise SafetyViolation("Test database name is not a reviewed disposable clone.")
        return "kpdesk:accounts-http:" + name.rsplit("_", 1)[-1]

    def _owned_target(self, name: str) -> None:
        self.marker(name)
        if name not in self.owned or name not in self.marked:
            raise SafetyViolation("Only marked clones created by this pytest session are allowed.")

    def _metadata(self, cursor: Any, query: str, parameters: Any = None) -> Any:
        previous = getattr(self._metadata_state, "active", False)
        row_factory = cursor.row_factory
        self._metadata_state.active = True
        try:
            cursor.row_factory = tuple_row
            cursor.execute(query, parameters)
            return cursor.fetchone()
        finally:
            cursor.row_factory = row_factory
            self._metadata_state.active = previous

    def _identity(self, cursor: Any, name: str) -> None:
        row = self._metadata(cursor, _IDENTITY)
        valid = (
            row is not None and tuple(row[:2]) == ("postgres", "kpdesk")
            if name == "postgres" else
            row is not None and tuple(row) == (name, "kpdesk", "kpdesk", SessionSafety.marker(name))
        )
        if not valid:
            raise SafetyViolation("PostgreSQL test identity, owner or marker mismatch.")

    def connect(self, conninfo: str = "", **kwargs: Any) -> Any:
        if not self.allow_postgres:
            raise SafetyViolation("Pytest is offline; physical database connections are denied.")
        if any(os.environ.get(key) for key in (
            "PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS", "PGPASSFILE",
        )):
            raise SafetyViolation("Ambient libpq connection overrides are forbidden during pytest.")
        driver_options = {"autocommit", "row_factory", "context", "prepare_threshold", "cursor_factory"}
        try:
            values = conninfo_to_dict(conninfo, **{k: v for k, v in kwargs.items() if k not in driver_options})
        except (psycopg.Error, TypeError, ValueError):
            raise SafetyViolation("Invalid test database connection settings.") from None
        if (
            values.get("host") != "127.0.0.1" or str(values.get("port")) != "55432"
            or values.get("user") != "kpdesk"
            or any(values.get(key) for key in ("options", "service", "hostaddr", "passfile"))
        ):
            raise SafetyViolation("Only the reviewed local PostgreSQL owner connection is allowed.")
        name = values.get("dbname", "")
        if not isinstance(name, str):
            raise SafetyViolation("Test database name is unavailable.")
        if name != "postgres":
            self._owned_target(name)
        connection = self._connector(conninfo, **kwargs)
        try:
            with connection.cursor() as cursor:
                self._identity(cursor, name)
            connection.rollback()  # No caller transaction exists at physical connect time.
        except (SafetyViolation, psycopg.Error, TypeError, ValueError, IndexError):
            connection.close()
            raise SafetyViolation("PostgreSQL test connection identity could not be verified.") from None
        return connection

    def _do_connect(self, dialect: Any, record: Any, args: Any, kwargs: Any) -> None:
        if not self.allow_postgres or dialect.name != "postgresql":
            raise SafetyViolation("Pytest database access requires the reviewed clone SQL profile.")

    def before_execute(self, connection: Any, cursor: Any, statement: str,
                       parameters: Any, context: Any, executemany: bool) -> None:
        if not self.allow_postgres:
            raise SafetyViolation("Physical SQL is forbidden in the offline test profile.")
        name = connection.engine.url.database
        if not isinstance(name, str):
            raise SafetyViolation("Test database identity is unavailable.")
        if name != "postgres":
            self._owned_target(name)
            if _SAVEPOINT_ROLLBACK.fullmatch(statement.strip().removesuffix(";").strip()):
                # A failed SQL transaction refuses metadata SELECT until rollback.
                # This exact recovery command cannot write data or change a marker;
                # every subsequent statement resumes the full metadata check.
                if (cursor.connection.info.dbname, cursor.connection.info.user) != (name, "kpdesk"):
                    raise SafetyViolation("Savepoint recovery requires the actual owned clone connection.")
                return
            self._identity(cursor, name)
            # Remove comments before checking, including SQL emitted by migrations.
            sql = re.sub(r"/\*.*?\*/|--[^\r\n]*", " ", statement, flags=re.DOTALL)
            if _CLUSTER_SQL.search(sql):
                raise SafetyViolation("Cluster-wide SQL is forbidden even inside a disposable clone.")
            return
        self._identity(cursor, "postgres")
        sql = statement.strip().removesuffix(";").strip()
        if "".join(sql.lower().split()) in _ADMIN_READS:
            return
        create, comment, drop = _CREATE.fullmatch(sql), _COMMENT.fullmatch(sql), _DROP.fullmatch(sql)
        if create:
            self.marker(create[1])
            if create[1] in self.owned:
                raise SafetyViolation("Duplicate clone provisioning is forbidden.")
            return
        if comment:
            target = comment[1]
            if target not in self.owned or target in self.marked or comment[2] != self.marker(target):
                raise SafetyViolation("Only initial marking of an owned clone is allowed.")
            expected_marker = None
        elif drop:
            target = drop[1]
            self._owned_target(target)
            expected_marker = self.marker(target)
        else:
            raise SafetyViolation("Unreviewed SQL on the PostgreSQL admin database is forbidden.")
        row = self._metadata(cursor, _TARGET, (target,))
        if row is None or tuple(row) != ("postgres", "kpdesk", "kpdesk", expected_marker):
            raise SafetyViolation("Refusing clone mutation after owner or marker change.")

    def after_execute(self, connection: Any, cursor: Any, statement: str,
                      parameters: Any, context: Any, executemany: bool) -> None:
        if connection.engine.url.database != "postgres":
            return
        sql = statement.strip().removesuffix(";").strip()
        create, comment, drop = _CREATE.fullmatch(sql), _COMMENT.fullmatch(sql), _DROP.fullmatch(sql)
        if create:
            self.owned.add(create[1])
        elif comment:
            self.marked.add(comment[1])
        elif drop:
            self.owned.discard(drop[1])
            self.marked.discard(drop[1])

    @staticmethod
    def _query_text(cursor: Any, query: Any) -> str:
        if isinstance(query, str):
            return query
        if isinstance(query, bytes):
            return query.decode("utf-8")
        try:
            return str(query.as_string(cursor.connection))
        except (AttributeError, psycopg.Error, TypeError, ValueError):
            raise SafetyViolation("Unsupported SQL representation in test profile.") from None

    def execute_raw(self, cursor: Any, query: Any, parameters: Any = None, **kwargs: Any) -> Any:
        if getattr(self._metadata_state, "active", False):
            return self._execute(cursor, query, parameters, **kwargs)
        connection = SimpleNamespace(engine=SimpleNamespace(url=SimpleNamespace(database=cursor.connection.info.dbname)))
        statement = self._query_text(cursor, query)
        self.before_execute(connection, cursor, statement, parameters, None, False)
        result = self._execute(cursor, query, parameters, **kwargs)
        self.after_execute(connection, cursor, statement, parameters, None, False)
        return result

    def executemany_raw(self, cursor: Any, query: Any, parameters: Any, **kwargs: Any) -> Any:
        if cursor.connection.info.dbname == "postgres":
            raise SafetyViolation("Bulk SQL on the test admin connection is forbidden.")
        connection = SimpleNamespace(engine=SimpleNamespace(url=SimpleNamespace(database=cursor.connection.info.dbname)))
        statement = self._query_text(cursor, query)
        self.before_execute(connection, cursor, statement, parameters, None, True)
        return self._executemany(cursor, query, parameters, **kwargs)

    @staticmethod
    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise SafetyViolation("Real network/provider access is forbidden during pytest.")

    @staticmethod
    async def async_blocked(*args: Any, **kwargs: Any) -> Any:
        return SessionSafety.blocked()

    def start(self) -> None:
        if self._started:
            raise RuntimeError("Test safety session is already active.")
        local = threading.local()
        original_pair, original_connect = socket.socketpair, socket.socket.connect
        original_connect_ex = socket.socket.connect_ex

        def control_pair(*args: Any, **kwargs: Any) -> Any:
            local.control_pipe = True
            try:
                return original_pair(*args, **kwargs)
            finally:
                local.control_pipe = False

        def guarded_connect(sock: socket.socket, address: Any) -> Any:
            if getattr(local, "control_pipe", False):
                return original_connect(sock, address)
            return self.blocked()

        def guarded_connect_ex(sock: socket.socket, address: Any) -> Any:
            if getattr(local, "control_pipe", False):
                return original_connect_ex(sock, address)
            return self.blocked()

        def connect_class(cls: Any, /, conninfo: str = "", **kwargs: Any) -> Any:
            return self.connect(conninfo, **kwargs)

        self._stack.enter_context(patch.object(socket, "socketpair", control_pair))
        self._stack.enter_context(patch.object(socket.socket, "connect", guarded_connect))
        self._stack.enter_context(patch.object(socket.socket, "connect_ex", guarded_connect_ex))
        self._stack.enter_context(patch.object(socket.socket, "sendto", self.blocked))
        if hasattr(socket.socket, "sendmsg"):
            self._stack.enter_context(patch.object(socket.socket, "sendmsg", self.blocked))
        self._stack.enter_context(patch.object(httpx.HTTPTransport, "handle_request", self.blocked))
        self._stack.enter_context(patch.object(httpx.AsyncHTTPTransport, "handle_async_request", self.async_blocked))
        self._stack.enter_context(patch.object(psycopg, "connect", self.connect))
        self._stack.enter_context(patch.object(psycopg.Connection, "connect", classmethod(connect_class)))
        self._stack.enter_context(patch.object(psycopg.AsyncConnection, "connect", self.async_blocked))
        guard = self

        def execute_cursor(cursor: Any, query: Any, parameters: Any = None, **kwargs: Any) -> Any:
            return guard.execute_raw(cursor, query, parameters, **kwargs)

        def executemany_cursor(cursor: Any, query: Any, parameters: Any, **kwargs: Any) -> Any:
            return guard.executemany_raw(cursor, query, parameters, **kwargs)

        self._stack.enter_context(patch.object(psycopg.Cursor, "execute", execute_cursor))
        self._stack.enter_context(patch.object(psycopg.Cursor, "executemany", executemany_cursor))
        for name, handler in (("do_connect", self._do_connect),
                              ("before_cursor_execute", self.before_execute),
                              ("after_cursor_execute", self.after_execute)):
            event.listen(Engine, name, handler)
            self._stack.callback(event.remove, Engine, name, handler)
        self._started = True

    def close(self) -> None:
        self._stack.close()
        self._started = False
