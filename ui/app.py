import logging
import os
import sys
from datetime import timedelta
from functools import wraps

# Allow `from backend...` imports when running ui/app.py directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import requests
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, session, stream_with_context, url_for)
from flask_session import Session

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App config
# ---------------------------------------------------------------------------
app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("APP_SECRET_KEY", "dev-secret")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("ENVIRONMENT") == "production"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=24)
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_FILE_DIR"] = os.path.join(os.path.dirname(__file__), "..", "flask_session")
app.config["SESSION_PERMANENT"] = True

os.makedirs(app.config["SESSION_FILE_DIR"], exist_ok=True)
Session(app)

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
MILLPONT_ACCOUNT_ID = "22bcddb8-fee7-4fb2-b03a-f8136c0f44b3"

# ---------------------------------------------------------------------------
# Auth0
# ---------------------------------------------------------------------------
oauth = OAuth(app)
auth0 = oauth.register(
    "auth0",
    client_id=os.environ.get("AUTH0_CLIENT_ID"),
    client_secret=os.environ.get("AUTH0_CLIENT_SECRET"),
    api_base_url=f'https://{os.environ.get("AUTH0_DOMAIN")}',
    access_token_url=f'https://{os.environ.get("AUTH0_DOMAIN")}/oauth/token',
    authorize_url=f'https://{os.environ.get("AUTH0_DOMAIN")}/authorize',
    jwks_uri=f'https://{os.environ.get("AUTH0_DOMAIN")}/.well-known/jwks.json',
    server_metadata_url=f'https://{os.environ.get("AUTH0_DOMAIN")}/.well-known/openid-configuration',
    client_kwargs={"scope": "openid profile email"},
)


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "profile" not in session:
            session["next_url"] = request.url
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def _account_headers() -> dict:
    """Build X-Account-ID / X-Is-Admin headers from the current session."""
    return {
        "X-Account-ID": session.get("account_id") or "",
        "X-Is-Admin": "true" if session.get("is_admin") else "false",
        "X-User-Email": session.get("profile", {}).get("email", ""),
    }


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/login")
def login():
    if request.args.get("clear_session") == "1":
        session.clear()
    return auth0.authorize_redirect(
        redirect_uri=os.environ.get("AUTH0_CALLBACK_URL", "http://localhost:5001/callback"),
        response_type="code",
        prompt="login",
    )


@app.route("/callback")
def callback():
    try:
        token = auth0.authorize_access_token()
        userinfo = auth0.get("userinfo").json()

        session["profile"] = {
            "user_id": userinfo["sub"],
            "name": userinfo.get("name", ""),
            "picture": userinfo.get("picture", ""),
            "email": userinfo.get("email", ""),
        }

        # Look up (or create) the user in auth0_users to get their account_id
        try:
            from backend.services.db import get_user_account, upsert_user

            full_name = userinfo.get("name", "")
            parts = full_name.split(" ", 1)
            first_name, last_name = parts[0], (parts[1] if len(parts) > 1 else "")

            upsert_user(
                auth0_id=userinfo["sub"],
                email=userinfo.get("email", ""),
                first_name=first_name,
                last_name=last_name,
            )

            user = get_user_account(userinfo["sub"])
            account_id = str(user["account_id"]) if user and user.get("account_id") else None
            account_name = user["account_name"] if user and user.get("account_name") else None
        except Exception as exc:
            logger.warning(f"DB lookup failed during callback (continuing): {exc}")
            account_id = None
            account_name = None

        session["account_id"] = account_id
        session["account_name"] = account_name
        session["is_admin"] = (account_id == MILLPONT_ACCOUNT_ID)

        next_url = session.pop("next_url", "/")
        return redirect(next_url)

    except Exception as exc:
        err = str(exc)
        logger.error(f"Auth0 callback error: {err}")
        if "mismatching_state" in err or "state" in err.lower():
            session.clear()
            return redirect(url_for("login") + "?clear_session=1")
        return redirect(url_for("login"))


@app.route("/logout")
def logout():
    session.clear()
    return render_template("logout.html")


# ---------------------------------------------------------------------------
# Upload + session init
# ---------------------------------------------------------------------------

@app.route("/")
@requires_auth
def index():
    return render_template(
        "index.html",
        user=session.get("profile", {}),
        account_name=session.get("account_name", ""),
        is_admin=session.get("is_admin", False),
    )


@app.route("/upload", methods=["POST"])
@requires_auth
def upload():
    if "file" not in request.files:
        return render_template("index.html", error="No file selected.",
                               user=session.get("profile", {}),
                               account_name=session.get("account_name", ""),
                               is_admin=session.get("is_admin", False))

    f = request.files["file"]
    if not f.filename:
        return render_template("index.html", error="No file selected.",
                               user=session.get("profile", {}),
                               account_name=session.get("account_name", ""),
                               is_admin=session.get("is_admin", False))

    try:
        resp = requests.post(
            f"{BACKEND_URL}/api/upload",
            files={"file": (f.filename, f.read(), f.content_type or "application/octet-stream")},
            headers=_account_headers(),
            timeout=60,
        )
        if not resp.ok:
            # Surface the backend's detail message to the user
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise requests.HTTPError(detail, response=resp)
        data = resp.json()
    except requests.RequestException as exc:
        err_msg = str(exc)
        return render_template("index.html", error=err_msg,
                               user=session.get("profile", {}),
                               account_name=session.get("account_name", ""),
                               is_admin=session.get("is_admin", False))

    session["session_id"] = data["session_id"]

    # Fetch opening message
    try:
        opening_resp = requests.get(
            f"{BACKEND_URL}/api/session/{data['session_id']}/opening",
            timeout=15,
        )
        opening_msg = opening_resp.json().get("message", "") if opening_resp.ok else ""
    except Exception:
        opening_msg = ""

    return render_template(
        "chat.html",
        session_id=data["session_id"],
        file_name=data["file_name"],
        feature_count=data["feature_count"],
        features=data["features"],
        opening_message=opening_msg,
        user=session.get("profile", {}),
        account_name=session.get("account_name", ""),
        is_admin=session.get("is_admin", False),
    )


# ---------------------------------------------------------------------------
# Chat (streaming proxy)
# ---------------------------------------------------------------------------

@app.route("/chat", methods=["POST"])
@requires_auth
def chat():
    body = request.get_json()
    session_id = body.get("session_id") or session.get("session_id")
    message = body.get("message", "")

    if not session_id or not message:
        return jsonify({"error": "Missing session_id or message"}), 400

    def generate():
        try:
            with requests.post(
                f"{BACKEND_URL}/api/agent/chat",
                json={"session_id": session_id, "message": message},
                headers=_account_headers(),
                stream=True,
                timeout=120,
            ) as r:
                for line in r.iter_lines():
                    if line:
                        yield line.decode() + "\n\n"
        except Exception as exc:
            import json
            yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


# ---------------------------------------------------------------------------
# Risk check
# ---------------------------------------------------------------------------

@app.route("/insights/enrich", methods=["POST"])
@requires_auth
def insights_enrich():
    body = request.get_json()
    session_id = body.get("session_id") or session.get("session_id")
    if not session_id:
        return jsonify({"error": "Missing session_id"}), 400
    try:
        resp = requests.post(
            f"{BACKEND_URL}/api/insights/enrich",
            params={"session_id": session_id},
            headers=_account_headers(),
            timeout=120,
        )
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.RequestException as exc:
        try:
            detail = exc.response.json().get("detail", str(exc))
        except Exception:
            detail = str(exc)
        return jsonify({"error": detail}), 500


@app.route("/risk-check", methods=["POST"])
@requires_auth
def risk_check():
    body = request.get_json()
    session_id = body.get("session_id") or session.get("session_id")
    if not session_id:
        return jsonify({"error": "Missing session_id"}), 400

    try:
        resp = requests.post(
            f"{BACKEND_URL}/api/sources/risk-check",
            params={"session_id": session_id},
            headers=_account_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.RequestException as exc:
        try:
            detail = exc.response.json().get("detail", str(exc))
        except Exception:
            detail = str(exc)
        return jsonify({"error": detail}), 500


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

@app.route("/export/<session_id>")
@requires_auth
def export(session_id):
    try:
        resp = requests.get(
            f"{BACKEND_URL}/api/session/{session_id}/export",
            headers=_account_headers(),
            timeout=15,
        )
        if resp.status_code == 404:
            return jsonify({"error": "Export not ready"}), 404
        resp.raise_for_status()
        return Response(
            resp.content,
            mimetype="application/json",
            headers={
                "Content-Disposition": resp.headers.get(
                    "Content-Disposition",
                    f'attachment; filename="meti_{session_id[:8]}.json"',
                )
            },
        )
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True, port=5001)
