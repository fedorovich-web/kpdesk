"""Read-only runtime integrity and authorization assertions."""
from sqlalchemy import text
from sqlalchemy.orm import Session

from kpdesk.catalog.storage import get_active_catalog
from kpdesk.db.session import app_engine, assert_unprivileged_session

rights = {
    "catalog_versions": {"SELECT","INSERT"},
    "catalog_items": {"SELECT","INSERT"},
    "catalog_active": {"SELECT","UPDATE"},
    "idempotency_records": {"SELECT","INSERT"},
    "quote_documents": {"SELECT","INSERT","UPDATE"},
    "quote_revisions": {"SELECT","INSERT"},
    "quote_write_requests": {"SELECT","INSERT"},
}
engine=app_engine()
with Session(engine) as session:
    assert_unprivileged_session(session)
    principal=session.execute(text("SELECT current_user,current_database()")).one()
    assert principal == ("kpdesk_app","kpdesk"),principal
    for table,allowed in rights.items():
        for permission in ("SELECT","INSERT","UPDATE","DELETE","TRUNCATE","REFERENCES","TRIGGER","MAINTAIN"):
            granted=session.scalar(text("SELECT has_table_privilege(:username,:tab,:perm)"),{
                "username":"kpdesk_app","tab":"public."+table,"perm":permission})
            assert bool(granted) is (permission in allowed),(table,permission,granted)
    assert not session.scalar(text("SELECT has_schema_privilege('kpdesk_app','public','CREATE')"))
    assert not session.scalar(text("SELECT has_database_privilege('kpdesk_app','kpdesk','CREATE')"))
    assert not session.scalar(text("SELECT rolsuper FROM pg_roles WHERE rolname='kpdesk_app'"))
    active=get_active_catalog(session)
    print("RUNTIME_ROLE_CONNECTED",principal)
    print("LEAST_PRIVILEGE_CHECKS_PASS",len(rights),"tables")
    print("ACTIVE_CATALOG_PRESENT",active is not None)
    session.rollback()
engine.dispose()
