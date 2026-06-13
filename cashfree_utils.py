import aiohttp
import logging
from config import config

logger = logging.getLogger(__name__)

async def create_payment_order(
    order_id: str,
    amount: float,
    customer_id: str,
    customer_name: str,
    customer_phone: str,
    upi_only: bool = False
) -> dict:
    """Create Cashfree payment order and return payment link"""

    headers = {
        "x-client-id": config.CASHFREE_APP_ID,
        "x-client-secret": config.CASHFREE_SECRET_KEY,
        "x-api-version": "2023-08-01",
        "Content-Type": "application/json"
    }

    payload = {
        "order_id": order_id,
        "order_amount": round(amount, 2),
        "order_currency": "INR",
        "customer_details": {
            "customer_id": customer_id,
            "customer_name": customer_name,
            "customer_phone": customer_phone if len(customer_phone) == 10 else "9999999999"
        },
        "order_meta": {
            "return_url": f"https://t.me/{config.BOT_NAME.replace(' ', '')}",
            "notify_url": f"{config.WEBHOOK_URL}/cashfree/webhook" if config.WEBHOOK_URL else ""
        }
    }

    if upi_only:
        payload["order_meta"]["payment_methods"] = "upi"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{config.CASHFREE_BASE_URL}/orders",
                headers=headers,
                json=payload
            ) as resp:
                data = await resp.json()
                logger.info(f"Cashfree order response: {data}")

                if data.get("payment_session_id"):
                    # Get payment link
                    payment_link = data.get("order_meta", {}).get("payment_link")
                    if not payment_link:
                        # Use checkout URL
                        if config.CASHFREE_ENV == "sandbox":
                            payment_link = f"https://sandbox.cashfree.com/pg/view/sessions/{data['payment_session_id']}"
                        else:
                            payment_link = f"https://payments.cashfree.com/forms/pg-checkout?session_id={data['payment_session_id']}"
                    return {"payment_link": payment_link, "session_id": data["payment_session_id"]}

                return {"error": data.get("message", "Unknown error")}

    except Exception as e:
        logger.error(f"Cashfree error: {e}")
        return {"error": str(e)}


def verify_payment_signature(order_id: str, order_amount: str, reference_id: str,
                              payment_status: str, signature: str, timestamp: str) -> bool:
    """Verify Cashfree payment signature"""
    import hmac
    import hashlib
    data = f"{order_id}{order_amount}{reference_id}{payment_status}"
    expected = hmac.new(
        config.CASHFREE_SECRET_KEY.encode(),
        data.encode(),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
