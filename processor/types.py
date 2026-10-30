"""共享类型定义（不依赖任何具体 processor 实现，避免循环导入）。"""
from enum import Enum


class Direction(Enum):
    """渐变 / 拼接方向"""
    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"
    DIAGONAL = "diagonal"
    RADIAL = "radial"


class Alignment(Enum):
    """对齐方式"""
    START = "start"
    CENTER = "center"
    END = "end"

    # 水平拼接时的垂直对齐别名
    TOP = "start"
    MIDDLE = "center"
    BOTTOM = "end"

    # 垂直拼接时的水平对齐别名
    LEFT = "start"
    RIGHT = "end"
