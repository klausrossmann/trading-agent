import httpx
import structlog
from pydantic import SecretStr

log = structlog.get_logger(__name__)

TIMEOUT_S = 10.0


async def ping(client: httpx.AsyncClient, url: SecretStr | None) -> bool:
    """Ping the external heartbeat service. Never raises: a failed ping must not stop the agent."""
    if url is None or not url.get_secret_value():
        log.debug("heartbeat.skipped", reason="HEARTBEAT_URL not set")
        return False
    try:
        response = await client.get(url.get_secret_value(), timeout=TIMEOUT_S)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        log.warning("heartbeat.failed", status=exc.response.status_code)
        return False
    except httpx.HTTPError as exc:
        # The URL is a credential, so log only the error type.
        log.warning("heartbeat.failed", error=type(exc).__name__)
        return False
    log.debug("heartbeat.ok")
    return True
