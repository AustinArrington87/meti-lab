import psycopg2
import psycopg2.extras
from backend.config import settings


def get_db_connection():
    return psycopg2.connect(
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        host=settings.db_host,
        port=settings.db_port,
        sslmode="require",
        connect_timeout=10,
        options="-c statement_timeout=15000",
    )


def get_user_account(auth0_id: str) -> dict | None:
    """
    Look up a user in auth0_users and return their account details.
    Returns None if the user is not found.
    """
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT u.id, u.auth0_id, u.email, u.first_name, u.last_name,
                       u.user_type, u.account_id, a.name AS account_name
                FROM auth0_users u
                LEFT JOIN accounts a ON a.id = u.account_id
                WHERE u.auth0_id = %s
                """,
                (auth0_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def upsert_user(auth0_id: str, email: str, first_name: str, last_name: str) -> dict:
    """
    Insert a new auth0_users row if the user doesn't exist yet.
    account_id is left NULL until an admin links them in the DB.
    Returns the user row dict.
    """
    import uuid as _uuid

    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO auth0_users (id, auth0_id, email, first_name, last_name, user_type, is_active)
                VALUES (%s, %s, %s, %s, %s, 'user', TRUE)
                ON CONFLICT (auth0_id) DO UPDATE
                    SET email = EXCLUDED.email,
                        first_name = EXCLUDED.first_name,
                        last_name = EXCLUDED.last_name,
                        updated_at = NOW()
                RETURNING id, auth0_id, email, first_name, last_name, user_type, account_id
                """,
                (str(_uuid.uuid4()), auth0_id, email, first_name, last_name),
            )
            conn.commit()
            return dict(cur.fetchone())
    finally:
        conn.close()
