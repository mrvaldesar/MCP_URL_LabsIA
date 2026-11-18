import asyncio
from contextlib import asynccontextmanager, suppress

from starlette.concurrency import run_in_threadpool

from src import database, github
from src.audit import logger
from src.auth import configured_limit


def max_attempts() -> int:
    return configured_limit("GITHUB_RETRY_MAX_ATTEMPTS", 5)


def stale_minutes() -> int:
    return configured_limit("GITHUB_PENDING_ALERT_MINUTES", 15)


async def retry(receipt_id: int, section: str | None, actor: str):
    limit = max_attempts()
    outcome, claimed = await run_in_threadpool(database.claim_verification, receipt_id, section, limit)
    if claimed is None:
        return outcome, None
    repository, label, sha, count = claimed
    logger.info("Reintento de verificación iniciado actor=%s recibo=%d reintento=%d", actor, receipt_id, count)
    ci = "desconocido"
    try:
        verified_sha = await github.verify_submission(repository, label, sha)
        ci = await github.verified_ci_status(repository, verified_sha)
    except github.GitHubUnavailable:
        status = "pendiente"
    except github.GitHubVerificationError:
        status = "inconsistente"
    else:
        sha, status = verified_sha, "verificado"
    updated = await run_in_threadpool(database.finish_verification, receipt_id, count, sha, status, ci)
    if not updated:
        return "cambió", None
    logger.info("Verificación reintentada actor=%s recibo=%d reintento=%d estado=%s",
                actor, receipt_id, count, status)
    if status == "pendiente" and count == limit:
        logger.warning("Recibo pendiente agotó sus reintentos recibo=%d reintentos=%d", receipt_id, count)
    return status, {"receipt_id": receipt_id, "verification_status": status,
                    "verified_ci_status": ci, "verification_retry_count": count}


async def retry_batch():
    threshold = stale_minutes()
    aged = await run_in_threadpool(database.aged_pending_count, threshold)
    if aged:
        logger.warning("Recibos pendientes de GitHub por más de %d minutos cantidad=%d", threshold, aged)
    for receipt_id in await run_in_threadpool(database.pending_verification_ids, max_attempts()):
        await retry(receipt_id, None, "reintento_automático")


async def _loop(interval: int):
    while True:
        await asyncio.sleep(interval)
        try:
            await retry_batch()
        except Exception:
            logger.exception("Falló el ciclo de reintentos de GitHub")


@asynccontextmanager
async def background_retries():
    interval = configured_limit("GITHUB_RETRY_INTERVAL_SECONDS", 60)
    max_attempts()
    stale_minutes()
    task = asyncio.create_task(_loop(interval))
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
