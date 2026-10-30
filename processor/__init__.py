"""处理器包：导入本包时自动发现并注册所有具体处理器。

注意：PyInstaller 无法追踪 importlib 的动态导入，
打包时必须在 spec 的 hiddenimports 中显式声明下面三个模块。
"""
import importlib
import inspect

from core.logger import logger
from processor.core import ImageProcessor, get_all_processors, register_processor

_MODULES = (
    "processor.filters",
    "processor.generators",
    "processor.mergers",
)


def _auto_register_processors():
    for module_name in _MODULES:
        try:
            importlib.import_module(module_name)
        except ImportError as e:  # noqa: BLE001
            logger.warning(f"Failed to import {module_name}: {e}")


def _walk_subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from _walk_subclasses(sub)


def _register_concrete_processors():
    """遍历所有子类，注册「可实例化且 name 非空」的具体处理器。

    抽象基类（FilterProcessor / Generator / Merger）会被自动跳过。
    """
    for cls in _walk_subclasses(ImageProcessor):
        if inspect.isabstract(cls):
            continue
        try:
            key = cls().name()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Skip processor {cls.__name__}: {e}")
            continue
        if key:
            register_processor(key, cls)


def _register_heif():
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        logger.debug("Registered plugin: pillow_heif")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"pillow_heif unavailable, HEIC support disabled: {e}")


_auto_register_processors()
_register_concrete_processors()
_register_heif()

logger.debug(f"Processors registered: {sorted(get_all_processors().keys())}")
