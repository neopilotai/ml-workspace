import os
import sys

from loguru import logger


def setup_logging():
    # Remove default handler
    """
    Configure the module-level Loguru logger according to environment variables.
    
    When called, this function resets existing Loguru handlers and adds a stdout handler that either emits JSON or a human-readable formatted log line depending on the WORKSPACE_LOG_JSON environment variable. The log level is taken from WORKSPACE_LOG_LEVEL.
    
    Returns:
        logger: A Loguru logger configured using WORKSPACE_LOG_JSON (default "false") and WORKSPACE_LOG_LEVEL (default "INFO").
    """
    logger.remove()

    # Check if JSON logging is enabled
    use_json = os.getenv("WORKSPACE_LOG_JSON", "false").lower() == "true"
    log_level = os.getenv("WORKSPACE_LOG_LEVEL", "INFO").upper()

    if use_json:
        logger.add(sys.stdout, format="{message}", serialize=True, level=log_level)
    else:
        format = (
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | <cyan>{name}</cyan>:"
            "<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
        )
        logger.add(sys.stdout, format=format, level=log_level)

    return logger


# Initialize default logger
log = setup_logging()


def get_secret(name, default=None):
    """
    Retrieve a secret by name from the container secrets path or from an environment variable, preferring a secrets file.
    
    Parameters:
        name (str): The secret name; used as the filename under WORKSPACE_SECRETS_PATH and as the environment variable key.
        default (Optional[str]): Value to return if the secret file and environment variable are both absent.
    
    Returns:
        str or None: The secret value as a string if found, otherwise the provided default.
    """
    secret_path = os.path.join(
        os.getenv("WORKSPACE_SECRETS_PATH", "/run/secrets"), name
    )
    if os.path.exists(secret_path):
        try:
            with open(secret_path, "r") as f:
                log.info(f"Using secret from file: {name}")
                return f.read().strip()
        except Exception:
            log.error(f"Failed to read secret file: {name}")

    return os.getenv(name, default)