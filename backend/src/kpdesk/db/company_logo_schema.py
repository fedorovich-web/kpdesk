"""Additive 0039 logo contract, extending the frozen 0038 function guards."""
from sqlalchemy import Connection, text

from kpdesk.db.issuer_requisites_schema import SAVE as SAVE_0038
from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone

PROFILE_CONSTRAINT = "organization_proposal_profiles_profile_check"
MAX_PROFILE_BYTES = 131072

_LOGO_GUARD = """
 IF p_data IS NULL OR jsonb_typeof(p_data) IS DISTINCT FROM 'object' THEN RETURN NULL; END IF;
 IF octet_length(p_data::text)>131072 OR octet_length((p_data-'logo')::text)>12000 THEN RETURN NULL; END IF;
 logo := p_data->'logo';
 IF logo IS NOT NULL THEN
   IF jsonb_typeof(logo) IS DISTINCT FROM 'object' THEN RETURN NULL; END IF;
   IF (logo - ARRAY['mime_type','width','height','sha256','data_base64']) <> '{}'::jsonb
   OR logo->>'mime_type' IS DISTINCT FROM 'image/png'
   OR jsonb_typeof(logo->'width') IS DISTINCT FROM 'number'
   OR jsonb_typeof(logo->'height') IS DISTINCT FROM 'number'
   OR coalesce(logo->>'width','') !~ '^[0-9]{1,3}$'
   OR coalesce(logo->>'height','') !~ '^[0-9]{1,3}$'
   OR jsonb_typeof(logo->'sha256') IS DISTINCT FROM 'string'
   OR coalesce(logo->>'sha256','') !~ '^[0-9a-f]{64}$'
   OR jsonb_typeof(logo->'data_base64') IS DISTINCT FROM 'string'
   OR length(logo->>'data_base64') NOT BETWEEN 1 AND 87384
   OR length(logo->>'data_base64') % 4 <> 0
   OR logo->>'data_base64' !~ '^[A-Za-z0-9+/]*={0,2}$' THEN RETURN NULL; END IF;
   logo_w := (logo->>'width')::integer; logo_h := (logo->>'height')::integer;
   IF logo_w NOT BETWEEN 1 AND 640 OR logo_h NOT BETWEEN 1 AND 256 THEN RETURN NULL; END IF;
   BEGIN png := decode(logo->>'data_base64','base64');
   EXCEPTION WHEN data_exception THEN RETURN NULL; END;
   IF octet_length(png) NOT BETWEEN 57 AND 65536 THEN RETURN NULL; END IF;
   IF replace(encode(png,'base64'),E'\\n','') IS DISTINCT FROM logo->>'data_base64'
   OR encode(sha256(png),'hex') IS DISTINCT FROM logo->>'sha256'
   OR substring(png from 1 for 16) <> decode('89504e470d0a1a0a0000000d49484452','hex')
   OR substring(png from octet_length(png)-11 for 12) <> decode('0000000049454e44ae426082','hex')
   OR get_byte(png,24)<>8 OR get_byte(png,25)<>6
   OR get_byte(png,26)<>0 OR get_byte(png,27)<>0 OR get_byte(png,28)<>0
   OR get_byte(png,16)*16777216::bigint + get_byte(png,17)*65536 + get_byte(png,18)*256 + get_byte(png,19) <> logo_w
   OR get_byte(png,20)*16777216::bigint + get_byte(png,21)*65536 + get_byte(png,22)*256 + get_byte(png,23) <> logo_h
   THEN RETURN NULL; END IF;
 END IF;
"""

# 0038 is frozen. Reuse its session/owner/CAS checks verbatim.
SAVE = SAVE_0038.replace("weights integer[]; total integer; i integer;",
                        "weights integer[]; total integer; i integer; logo jsonb; png bytea; logo_w integer; logo_h integer;")
SAVE = SAVE.replace("BEGIN\n", "BEGIN\n" + _LOGO_GUARD, 1)
SAVE = SAVE.replace("octet_length(p_data::text)>12000", "octet_length(p_data::text)>131072")
SAVE = SAVE.replace("'generation_style','issuer_details']", "'generation_style','issuer_details','logo']")


def resize_profile_constraint(connection: Connection, maximum: int) -> None:
    if maximum not in (12000, MAX_PROFILE_BYTES):
        raise ValueError("Unsupported profile bound")
    connection.execute(text(f"ALTER TABLE public.organization_proposal_profiles DROP CONSTRAINT {PROFILE_CONSTRAINT}"))
    connection.execute(text(f"ALTER TABLE public.organization_proposal_profiles ADD CONSTRAINT {PROFILE_CONSTRAINT} "
                            f"CHECK(jsonb_typeof(profile)='object' AND octet_length(profile::text)<={maximum})"))


def install_company_logo_on_clone(connection: Connection) -> None:
    if (not connection.in_transaction() or not is_live_accounts_http_clone(connection)
        or connection.scalar(text("SELECT current_user")) != "kpdesk"):
        raise RuntimeError("Company logo installer requires tagged owner clone")
    resize_profile_constraint(connection, MAX_PROFILE_BYTES)
    connection.execute(text(SAVE))
