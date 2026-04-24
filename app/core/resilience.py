"""
resilience.py — Capa de resiliencia transversal.

Provee:
  • Decorador @with_retry  → reintento con exponential backoff + jitter.
  • CircuitBreaker          → evita llamadas a servicios caídos.
  • safe_call()             → wrapper genérico con fallback y alerta Telegram.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import random
import time
from enum import Enum
from typing import Any, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)
F = TypeVar("F", bound=Callable[..., Any])


# ─── Decorador de reintentos ──────────────────────────────────────────────────

def with_retry(
    max_attempts: int = 3,
    backoff_base: float = 2.0,
    backoff_max: float = 60.0,
    exceptions: tuple = (Exception,),
    on_fail_log: str = "Error en intento {attempt}/{max}: {exc}",
):
    """
    Decorador que reintenta una función sinc o async con exponential backoff + jitter.

    Uso:
        @with_retry(max_attempts=3, backoff_base=2)
        async def llamar_api(): ...
    """
    def decorator(func: F) -> F:
        is_async = asyncio.iscoroutinefunction(func)

        if is_async:
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                last_exc: Exception = Exception("Sin intento")
                for attempt in range(1, max_attempts + 1):
                    try:
                        return await func(*args, **kwargs)
                    except exceptions as exc:  # type: ignore[misc]
                        last_exc = exc
                        wait = min(backoff_base ** attempt + random.uniform(0, 1), backoff_max)
                        logger.warning(
                            on_fail_log.format(attempt=attempt, max=max_attempts, exc=exc)
                        )
                        if attempt < max_attempts:
                            await asyncio.sleep(wait)
                raise last_exc
            return async_wrapper  # type: ignore[return-value]
        else:
            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                last_exc: Exception = Exception("Sin intento")
                for attempt in range(1, max_attempts + 1):
                    try:
                        return func(*args, **kwargs)
                    except exceptions as exc:  # type: ignore[misc]
                        last_exc = exc
                        wait = min(backoff_base ** attempt + random.uniform(0, 1), backoff_max)
                        logger.warning(
                            on_fail_log.format(attempt=attempt, max=max_attempts, exc=exc)
                        )
                        if attempt < max_attempts:
                            time.sleep(wait)
                raise last_exc
            return sync_wrapper  # type: ignore[return-value]

    return decorator


# ─── Circuit Breaker ──────────────────────────────────────────────────────────

class CBState(str, Enum):
    CLOSED    = "CLOSED"      # Funcionando normalmente
    OPEN      = "OPEN"        # Demasiados fallos — rechaza llamadas
    HALF_OPEN = "HALF_OPEN"   # Probando si el servicio se recuperó


class CircuitBreaker:
    """
    Circuit Breaker simple para proteger servicios externos (Vertex AI, Meta API, etc.)

    Uso:
        cb = CircuitBreaker("vertex_ai", failure_threshold=5, recovery_timeout=60)

        if cb.allow():
            try:
                result = llamar_vertex_ai()
                cb.record_success()
            except Exception:
                cb.record_failure()
        else:
            use_fallback()
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ) -> None:
        self.name = name
        self._threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._failures = 0
        self._last_failure_time: float = 0.0
        self._state = CBState.CLOSED

    def allow(self) -> bool:
        """Retorna True si se puede realizar la llamada."""
        if self._state == CBState.CLOSED:
            return True
        if self._state == CBState.OPEN:
            if time.monotonic() - self._last_failure_time >= self._recovery_timeout:
                self._state = CBState.HALF_OPEN
                logger.info("CircuitBreaker '%s' → HALF_OPEN (prueba de recuperación)", self.name)
                return True
            return False
        # HALF_OPEN: permite un intento
        return True

    def record_success(self) -> None:
        self._failures = 0
        if self._state != CBState.CLOSED:
            logger.info("CircuitBreaker '%s' → CLOSED (recuperado)", self.name)
        self._state = CBState.CLOSED

    def record_failure(self) -> None:
        self._failures += 1
        self._last_failure_time = time.monotonic()
        if self._failures >= self._threshold or self._state == CBState.HALF_OPEN:
            if self._state != CBState.OPEN:
                logger.error(
                    "CircuitBreaker '%s' → OPEN (%d fallos consecutivos)",
                    self.name, self._failures,
                )
            self._state = CBState.OPEN

    @property
    def state(self) -> CBState:
        return self._state


# ─── Instancias globales de circuitos ────────────────────────────────────────

CB_VERTEX_AI  = CircuitBreaker("vertex_ai",  failure_threshold=4, recovery_timeout=90)
CB_META_API   = CircuitBreaker("meta_api",   failure_threshold=5, recovery_timeout=60)
CB_WICAPITAL  = CircuitBreaker("wicapital",  failure_threshold=3, recovery_timeout=900)  # 15 min


# ─── safe_call — wrapper genérico ─────────────────────────────────────────────

async def safe_call_async(
    fn: Callable[..., Any],
    *args: Any,
    fallback: Any = None,
    critical: bool = False,
    service_name: str = "servicio",
    circuit_breaker: Optional[CircuitBreaker] = None,
    **kwargs: Any,
) -> Any:
    """
    Ejecuta `fn` de forma segura. Si falla:
      - Retorna `fallback`.
      - Si `critical=True`, envía alerta roja a Telegram.
      - Registra falla en `circuit_breaker` si se provee.
    """
    if circuit_breaker and not circuit_breaker.allow():
        logger.warning("CircuitBreaker '%s' abierto — usando fallback.", circuit_breaker.name)
        return fallback

    try:
        result = await fn(*args, **kwargs) if asyncio.iscoroutinefunction(fn) else fn(*args, **kwargs)
        if circuit_breaker:
            circuit_breaker.record_success()
        return result
    except Exception as exc:
        logger.error("safe_call '%s' falló: %s", service_name, exc, exc_info=True)
        if circuit_breaker:
            circuit_breaker.record_failure()
        if critical:
            try:
                from app.services.crm_sync import TelegramAlerter  # import local para evitar circular
                TelegramAlerter().alerta_error_critico(service_name, str(exc))
            except Exception as tg_exc:
                logger.error("No se pudo enviar alerta Telegram: %s", tg_exc)
        return fallback
