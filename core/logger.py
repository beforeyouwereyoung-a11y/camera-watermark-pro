"""统一日志模块：loguru + 拦截标准 logging（Flask/Werkzeug）。

日志写入 DATA_ROOT/logs，打包后不依赖控制台。
"""
import logging
import sys

from loguru import logger

from core import LOG_DIR

logger.remove()

CONSOLE_FORMAT = (
    "<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | "
    "<cyan>{name}</cyan> - <level>{message}</level>"
)
FILE_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}"

_configured = False


class InterceptHandler(logging.Handler):
    """把标准 logging（Flask、Werkzeug 等）重定向到 loguru。"""

    def emit(self, record):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging(log_level: str = "INFO", enable_console: bool = True):
    global _configured
    if _configured:
        return logger

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # 打包为窗口程序后没有可用控制台，写入 stderr 可能抛错，做一次保护
    if enable_console and sys.stderr is not None:
        try:
            logger.add(sys.stderr, level=log_level, format=CONSOLE_FORMAT,
                       colorize=True, backtrace=False, diagnose=False)
        except Exception:
            pass

    logger.add(LOG_DIR / "app_{time:YYYY-MM-DD}.log", level=log_level,
               format=FILE_FORMAT, rotation="00:00", retention="14 days",
               encoding="utf-8", enqueue=True, backtrace=True, diagnose=False)
    logger.add(LOG_DIR / "error_{time:YYYY-MM-DD}.log", level="ERROR",
               format=FILE_FORMAT, rotation="00:00", retention="30 days",
               encoding="utf-8", enqueue=True)

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    _configured = True
    return logger


def init_from_config(config):
    debug = config.getboolean("DEFAULT", "debug", fallback=False)
    return setup_logging(log_level="DEBUG" if debug else "INFO")


__all__ = ["logger", "setup_logging", "init_from_config"]
