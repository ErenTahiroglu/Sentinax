import logging
import json
import urllib.request
import os
import traceback

class WebhookHandler(logging.Handler):
    def __init__(self, webhook_url):
        super().__init__()
        self.webhook_url = webhook_url

    def emit(self, record):
        if not self.webhook_url:
            return
        
        log_entry = self.format(record)
        payload = {
            "content": f"🚨 **Sentinax Worker Alert** 🚨\n**Level:** {record.levelname}\n**Logger:** {record.name}\n```\n{log_entry}\n```"
        }
        
        try:
            req = urllib.request.Request(
                self.webhook_url,
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json', 'User-Agent': 'Sentinax-Logger'}
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            # Fallback to console to avoid crashing the worker if webhook fails
            print(f"Failed to send webhook log: {e}")

def setup_logger():
    logger = logging.getLogger("sentinax_worker")
    logger.setLevel(logging.INFO)

    # Formatter for all handlers
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s')

    # 1. File Handler (worker.log)
    file_handler = logging.FileHandler("worker.log", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # 2. Console Handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 3. Webhook Handler (CRITICAL/ERROR only)
    webhook_url = os.environ.get("ALERT_WEBHOOK_URL")
    if webhook_url:
        webhook_handler = WebhookHandler(webhook_url)
        webhook_handler.setLevel(logging.ERROR) # Only send ERROR and CRITICAL to discord/telegram
        webhook_handler.setFormatter(formatter)
        logger.addHandler(webhook_handler)

    return logger

# Singleton logger instance
logger = setup_logger()
