import logging
import os


def configure_logging(component: str) -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format=f"%(asctime)s %(levelname)s [{component}] %(name)s: %(message)s",
    )
    # Third-party libraries are chatty at INFO (every HTTP request, every AMQP frame).
    for noisy in ("httpx", "httpcore", "aiormq", "aio_pika", "google_genai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
