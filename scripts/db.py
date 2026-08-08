"""
DB connection helper — altitude-upstream.

Workaround: database_url secret has a URL-encoding bug (special chars in password
truncate the parsed password). Always fetch password from the separate db_password
secret and connect with explicit keyword args + sslmode=require.
"""

import subprocess
from urllib.parse import urlparse
import psycopg2


def _get_secret(name: str) -> str:
    r = subprocess.run(
        [
            "aws", "secretsmanager", "get-secret-value",
            "--secret-id", name,
            "--region", os.environ.get("AWS_REGION", "us-east-1"),
            "--query", "SecretString",
            "--output", "text",
        ],
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def get_connection() -> psycopg2.extensions.connection:
    """Return an open psycopg2 connection to the altitude-upstream RDS instance."""
    url_str = _get_secret(os.environ["DB_URL_SECRET"])
    db_pass = _get_secret(os.environ["DB_PASSWORD_SECRET"])
    p = urlparse(url_str)
    return psycopg2.connect(
        host=p.hostname,
        port=p.port or 5432,
        dbname=p.path.lstrip("/"),
        user=p.username,
        password=db_pass,
        sslmode="require",
    )
