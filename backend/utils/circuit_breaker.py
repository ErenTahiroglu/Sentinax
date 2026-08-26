import time
import logging
from typing import Callable, Optional, Any, SupportsIndex, Union

logger = logging.getLogger(__name__)

class CircuitBreaker:
    def __init__(self, name: str, threshold: int = 3, timeout: int = 60, fallback_factory: Optional[Callable] = None):
        """
        Simple Circuit Breaker.
        
        Args:
            name: Name of the circuit (e.g., 'Yahoo', 'TEFAS')
            threshold: Number of consecutive failures before opening
            timeout: Seconds to wait before testing again (Half-Open)
            fallback_factory: Callable that returns a default/neutral value when Open
        """
        self.name = name
        self.threshold = threshold
        self.timeout = timeout
        self.fallback_factory = fallback_factory
        self.failures = 0
        self.state = "CLOSED" # CLOSED, OPEN, HALF_OPEN
        self.last_failure_time: float = 0.0

    def __call__(self, func: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            if self.state == "OPEN":
                if time.time() - self.last_failure_time > self.timeout:
                    logger.info(f"[{self.name}] Circuit HALF-OPEN, testing...")
                    self.state = "HALF_OPEN"
                else:
                    logger.warning(f"[{self.name}] Circuit OPEN. Fast-failing.")
                    if self.fallback_factory:
                        return self.fallback_factory()
                    raise Exception(f"Circuit Breaker [{self.name}] is OPEN")

            try:
                result = func(*args, **kwargs)
                if self.state == "HALF_OPEN":
                    logger.info(f"[{self.name}] Circuit CLOSED (Success)")
                    self.state = "CLOSED"
                    self.failures = 0
                return result
            except Exception as e:
                self.failures += 1
                self.last_failure_time = time.time()
                logger.warning(f"[{self.name}] Failure count: {self.failures}/{self.threshold}. Error: {str(e)[:100]}")
                
                if self.failures >= self.threshold:
                    if self.state != "OPEN":
                        logger.error(f"[{self.name}] Circuit OPENED! Fast-failing for {self.timeout}s.")
                    self.state = "OPEN"
                
                if self.fallback_factory:
                    return self.fallback_factory()
                raise e
        return wrapper

class FastFailList(list):
    """
    A list subclass that returns 0 for elements if the associated CircuitBreaker is OPEN.
    Used to bypass RETRY_BEKLEME sleeps when circuit is open.
    """
    def __init__(self, *args, cb: CircuitBreaker):
        super().__init__(*args)
        self.cb = cb
    
    def __getitem__(self, index: Union[SupportsIndex, slice]) -> Any:
        if self.cb and self.cb.state == "OPEN":
            return 0
        return super().__getitem__(index)

