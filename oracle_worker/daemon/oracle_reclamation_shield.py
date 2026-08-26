import time
import psutil
import hashlib
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("OracleShield")

def produce_load(duration_ms=50):
    """
    Produces a burst of CPU load using hashing.
    """
    start = time.perf_counter()
    while (time.perf_counter() - start) * 1000 < duration_ms:
        hashlib.sha256(os.urandom(1024)).hexdigest()

def monitor_and_shield():
    """
    Main loop to maintain CPU usage between 22% and 28%.
    This prevents Oracle from reclaiming the 'idle' instance.
    """
    logger.info("Oracle Reclamation Shield active. Targeting 22-28% CPU load.")
    
    TARGET_MIN = 22.0
    TARGET_MAX = 28.0
    
    try:
        while True:
            # Measure CPU usage over 1 second
            cpu_usage = psutil.cpu_percent(interval=1.0)
            
            if cpu_usage < TARGET_MIN:
                # Underloaded: Kick in synthetic load
                # We do a burst then re-check
                logger.debug(f"Current CPU: {cpu_usage}%. Adding load...")
                produce_load(duration_ms=300) 
            elif cpu_usage > TARGET_MAX:
                # Naturally loaded: Sleep and yield
                logger.debug(f"Current CPU: {cpu_usage}%. Sleeping...")
                time.sleep(2)
            else:
                # In target zone: Minimal sleep
                time.sleep(1)
                
    except KeyboardInterrupt:
        logger.info("Shield deactivated.")

if __name__ == "__main__":
    monitor_and_shield()
