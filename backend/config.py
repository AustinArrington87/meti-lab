import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

MILLPONT_ACCOUNT_ID = "22bcddb8-fee7-4fb2-b03a-f8136c0f44b3"


@dataclass
class Settings:
    claude_api_key: str
    auth0_domain: str
    auth0_client_id: str
    auth0_client_secret: str
    auth0_audience: str
    auth0_callback_url: str
    db_url: str
    db_name: str
    db_user: str
    db_password: str
    db_host: str
    db_port: str
    app_secret_key: str
    # Admin M2M credentials (full access — all accounts)
    meti_client_id: str
    meti_client_secret: str
    # Per-account M2M credentials keyed by account_id
    account_credentials: dict = field(default_factory=dict)


def get_settings() -> Settings:
    # Map account_id → (client_id, client_secret) for per-account METI API access
    account_credentials = {
        "02fe9a92-a3ce-4e63-9d62-c245d4641967": (  # Pro Cooperative
            os.environ.get("PROCOOP_CLIENT_ID", ""),
            os.environ.get("PROCOOP_CLIENT_SECRET", ""),
        ),
        "6e0927bd-e7f1-4b20-9dfc-8375a9c38387": (  # Arva Intelligence
            os.environ.get("ARVA_CLIENT_ID", ""),
            os.environ.get("ARVA_CLIENT_SECRET", ""),
        ),
        "b639306d-4542-4966-a992-3b79fc62d4f9": (  # Cargill
            os.environ.get("CARGILL_CLIENT_ID", ""),
            os.environ.get("CARGILL_CLIENT_SECRET", ""),
        ),
        "3d0ea6ef-28dd-44c6-b8a9-c84402c48452": (  # ESMC
            os.environ.get("ESMC_CLIENT_ID", ""),
            os.environ.get("ESMC_CLIENT_SECRET", ""),
        ),
        "8257b989-eff4-4dd4-99ac-927d20492146": (  # Kateri
            os.environ.get("KATERI_CLIENT_ID", ""),
            os.environ.get("KATERI_CLIENT_SECRET", ""),
        ),
        "4f66260c-f5ae-4ac1-87b3-c6a2f79f1472": (  # Perdue
            os.environ.get("PERDUE_CLIENT_ID", ""),
            os.environ.get("PERDUE_CLIENT_SECRET", ""),
        ),
        "8a40f643-9121-4483-9b8f-0c1daab18e2d": (  # RavahTech
            os.environ.get("RAVAH_CLIENT_ID", ""),
            os.environ.get("RAVAH_CLIENT_SECRET", ""),
        ),
        "e99c7326-48f9-4393-b414-a3f754ae1c2b": (  # Ryzo
            os.environ.get("RYZO_CLIENT_ID", ""),
            os.environ.get("RYZO_CLIENT_SECRET", ""),
        ),
        "7e509a16-7ca8-43a8-8620-8475a63f6ea0": (  # Colorado State Land Board
            os.environ.get("COLORADO_CLIENT_ID", ""),
            os.environ.get("COLORADO_CLIENT_SECRET", ""),
        ),
        "bc51658b-159c-4575-9ca6-f264e00826cd": (  # Cultivo
            os.environ.get("CULTIVO_CLIENT_ID", ""),
            os.environ.get("CULTIVO_CLIENT_SECRET", ""),
        ),
        "5dfdd4b5-3d77-45b9-b0fb-18eda6d13f6e": (  # Karbnz
            os.environ.get("KRBNZ_CLIENT_ID", ""),
            os.environ.get("KRBNZ_CLIENT_SECRET", ""),
        ),
    }

    return Settings(
        claude_api_key=os.environ["CLAUDE_API"],
        auth0_domain=os.environ.get("AUTH0_DOMAIN", ""),
        auth0_client_id=os.environ.get("AUTH0_CLIENT_ID", ""),
        auth0_client_secret=os.environ.get("AUTH0_CLIENT_SECRET", ""),
        auth0_audience=os.environ.get("AUTH0_AUDIENCE", "https://api.meti.millpont.com"),
        auth0_callback_url=os.environ.get("AUTH0_CALLBACK_URL", "http://localhost:5001/callback"),
        db_url=os.environ.get("DB_URL", ""),
        db_name=os.environ.get("DB_NAME", "postgres"),
        db_user=os.environ.get("DB_USER", ""),
        db_password=os.environ.get("DB_PASSWORD", ""),
        db_host=os.environ.get("DB_HOST", ""),
        db_port=os.environ.get("DB_PORT", "5432"),
        app_secret_key=os.environ.get("APP_SECRET_KEY", "dev-secret"),
        meti_client_id=os.environ.get("METI_CLIENT_ID", ""),
        meti_client_secret=os.environ.get("METI_CLIENT_SECRET", ""),
        account_credentials=account_credentials,
    )


settings = get_settings()
