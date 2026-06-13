import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # Bot
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    BOT_NAME: str = os.getenv("BOT_NAME", "SMM Panel Bot")
    ADMIN_IDS: list = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
    SUPPORT_USERNAME: str = os.getenv("SUPPORT_USERNAME", "support")

    # SMM Panel API
    SMM_API_URL: str = os.getenv("SMM_API_URL", "https://smmpanelone.com/api/v2")
    SMM_API_KEY: str = os.getenv("SMM_API_KEY", "")

    # Markup
    DEFAULT_MARKUP: float = 1 + float(os.getenv("DEFAULT_MARKUP_PERCENT", "20")) / 100
    TG_MEMBER_MARKUP: float = 1 + float(os.getenv("TG_MEMBER_MARKUP_PERCENT", "50")) / 100

    # UPI Manual Payment
    UPI_ID: str = os.getenv("UPI_ID", "yourname@upi")
    UPI_NAME: str = os.getenv("UPI_NAME", "Your Name")

    # Cashfree
    CASHFREE_APP_ID: str = os.getenv("CASHFREE_APP_ID", "")
    CASHFREE_SECRET_KEY: str = os.getenv("CASHFREE_SECRET_KEY", "")
    CASHFREE_ENV: str = os.getenv("CASHFREE_ENV", "production")  # production or sandbox

    @property
    def CASHFREE_BASE_URL(self) -> str:
        if self.CASHFREE_ENV == "sandbox":
            return "https://sandbox.cashfree.com/pg"
        return "https://api.cashfree.com/pg"

    # Server
    WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "")  # e.g. https://yourdomain.com
    PORT: int = int(os.getenv("PORT", 8080))

config = Config()
