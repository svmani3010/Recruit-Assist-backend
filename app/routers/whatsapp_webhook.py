import hmac
import hashlib
import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request, Response, HTTPException
from fastapi.responses import PlainTextResponse

from app.settings import get_settings, Settings
from app.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook/whatsapp", tags=["whatsapp_webhook"])


@router.get("", response_class=PlainTextResponse)
async def verify_webhook(
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """
    Step 4: Webhook Verification Handshake.
    Meta will issue a GET request to verify the endpoint.
    """
    mode = request.query_params.get("hub.mode")
    verify_token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode and verify_token:
        # Check the mode and token sent are correct
        if mode == "subscribe" and verify_token == settings.whatsapp_verify_token:
            logger.info("WhatsApp WEBHOOK_VERIFIED")
            return challenge
        else:
            # Responds with '403 Forbidden' if verify tokens do not match
            raise HTTPException(status_code=403, detail="Verification failed")
    
    raise HTTPException(status_code=400, detail="Missing parameters")


@router.post("")
async def receive_webhook(
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """
    Step 4 & 5: Handle incoming messages and delivery receipts securely.
    """
    # 1. Verify Signature to ensure the payload is genuinely from Meta
    signature_header = request.headers.get("x-hub-signature-256")
    if not signature_header:
        logger.warning("Missing x-hub-signature-256 in webhook headers")
        return Response(status_code=401)
    
    # Format of 'signature_header' is 'sha256=<hash>'
    parts = signature_header.split("sha256=")
    if len(parts) != 2:
        logger.warning("Invalid x-hub-signature-256 format")
        return Response(status_code=401)
    
    signature_hash = parts[1]
    body_bytes = await request.body()

    if settings.whatsapp_app_secret:
        expected_hash = hmac.new(
            key=settings.whatsapp_app_secret.encode("utf-8"),
            msg=body_bytes,
            digestmod=hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(signature_hash, expected_hash):
            logger.error("WhatsApp webhook signature validation failed")
            return Response(status_code=403, content="Validation failed")
    else:
        logger.warning("WHATSAPP_APP_SECRET is not configured; skipping signature verification!")

    # 2. Parse Payload
    try:
        payload = await request.json()
    except Exception:
        return Response(status_code=400, content="Invalid JSON")

    # The payload 'object' should be 'whatsapp_business_account'
    if payload.get("object") == "whatsapp_business_account":
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                
                # Retrieve Phone Number ID receiving the message
                phone_number_id = value.get("metadata", {}).get("phone_number_id")
                
                # Check for Message Status updates (Sent, Delivered, Read, Failed)
                for status in value.get("statuses", []):
                    logger.info("WhatsApp Status Update received: %s (id: %s)", status.get("status"), status.get("id"))
                    # TODO: Pass `phone_number_id` and `status` to conversation logging services
                
                # Check for Incoming Messages (Text, Media, etc)
                for message in value.get("messages", []):
                    sender_phone = message.get("from")
                    msg_text = message.get("text", {}).get("body", "") if message.get("type") == "text" else f"[{message.get('type')} message]"
                    logger.info("WhatsApp Message received from %s: %s", sender_phone, msg_text)
                    # TODO: Pass `phone_number_id` and `message` to conversational AI or routing
                    _log_incoming_message(value.get("contacts", []), message)
                    
        return Response(status_code=200, content="EVENT_RECEIVED")
    
    # Return a 404 for events not related to whatsapp_business_account
    raise HTTPException(status_code=404)


def _log_incoming_message(contacts: list, message: dict):
    """
    Saves a raw incoming message to the database for future reference.
    """
    try:
        db = get_db()
        sender_phone = message.get("from")
        sender_name = ""
        if contacts and len(contacts) > 0:
            sender_name = contacts[0].get("profile", {}).get("name", "")
            
        db["whatsapp_inbound"].insert_one({
            "wa_id": sender_phone,
            "profile_name": sender_name,
            "message_id": message.get("id"),
            "timestamp": message.get("timestamp"),
            "type": message.get("type"),
            "text": message.get("text", {}).get("body", ""),
            "raw_message": message
        })
    except Exception:
        logger.exception("Failed to insert incoming WhatsApp message to db")
