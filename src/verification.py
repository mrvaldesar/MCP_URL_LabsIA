import asyncio
from time import time
from contextlib import asynccontextmanager, suppress

from starlette.concurrency import run_in_threadpool

from src import database, github
from src.audit import logger
from src.auth import configured_limit


def max_attempts() -> int:
    return configured_limit("GITHUB_RETRY_MAX_ATTEMPTS", 5)


def stale_minutes() -> int:
    return configured_limit("GITHUB_PENDING_ALERT_MINUTES", 15)


def retry_window() -> int:
    return 3600 * configured_limit("GITHUB_RETRY_WINDOW_HOURS", 48)


def retry_interval() -> int:
    return configured_limit("GITHUB_RETRY_INTERVAL_SECONDS", 60)


async def retry(receipt_id: int, section: str | None, actor: str):
    limit = max_attempts()
    outcome, claimed = await run_in_threadpool(database.claim_verification, receipt_id, section, limit,
                                               retry_window(), github.credential_alert_active(),
                                               github.retry_not_before())
    if outcome != "tomado":
        return outcome, claimed
    repository, label, sha, count, lease = claimed
    next_at = 0
    logger.info("Reintento de verificación iniciado actor=%s recibo=%d reintento=%d", actor, receipt_id, count)
    ci = "desconocido"
    try:
        verified_sha = await github.verify_submission(repository, label, sha)
        ci = await github.verified_ci_status(repository, verified_sha)
    except github.GitHubCredentialError:
        count -= 1
        status = "pendiente"
    except github.GitHubUnavailable as exc:
        status = "pendiente"
        next_at = max(int(time()) + retry_interval() * 2 ** min(count - 1, 16), exc.retry_at)
    except github.GitHubVerificationError:
        status = "inconsistente"
    else:
        sha, status = verified_sha, "verificado"
    updated = await run_in_threadpool(database.finish_verification, receipt_id, lease, count, sha, status, ci, next_at)
    if not updated:
        return "cambió", None
    logger.info("Verificación reintentada actor=%s recibo=%d reintento=%d estado=%s",
                actor, receipt_id, count, status)
    if status == "pendiente" and count == limit:
        logger.warning("Recibo pendiente agotó sus reintentos recibo=%d reintentos=%d", receipt_id, count)
    return status, {"receipt_id": receipt_id, "verification_status": status,
                    "verified_ci_status": ci, "verification_retry_count": count}


async def retry_repositories():
    for team_id, repository, section in await run_in_threadpool(database.pending_repositories):
        if github.credential_alert_active() or github.retry_not_before():
            break
        next_at = 0
        try:
            await github.validate_repository(repository, section)
        except github.GitHubUnavailable as exc:
            status, next_at = "pendiente", max(int(time()) + retry_interval(), exc.retry_at)
        except ValueError:
            status = "inconsistente"
        else:
            status = "verificado"
        updated = await run_in_threadpool(database.update_repository_verification,
                                          team_id, repository, section, status, next_at)
        if updated:
            logger.info("Repositorio reintentado actor=reintento_automático equipo=%d estado=%s", team_id, status)


async def retry_batch():
    threshold = stale_minutes()
    aged = await run_in_threadpool(database.aged_pending_count, threshold)
    if aged:
        logger.warning("Recibos pendientes de GitHub por más de %d minutos cantidad=%d", threshold, aged)
    if github.retry_not_before():
        return
    await github.recover_credential()
    if github.credential_alert_active():
        return
    for receipt_id in await run_in_threadpool(database.pending_verification_ids, max_attempts(), retry_window()):
        await retry(receipt_id, None, "reintento_automático")
    await retry_repositories()


async def _loop(interval: int):
    while True:
        await asyncio.sleep(interval)
        try:
            await retry_batch()
        except Exception:
            logger.exception("Falló el ciclo de reintentos de GitHub")


@asynccontextmanager
async def background_retries():
    interval = retry_interval()
    retry_window()
    max_attempts()
    stale_minutes()
    task = asyncio.create_task(_loop(interval))
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
