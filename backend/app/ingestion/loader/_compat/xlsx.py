"""loader._compat.xlsx - openpyxl / pandas 探测（仅 XLSX 域需要）

两条读取通道：
- openpyxl：首选，能拿到合并单元格与样式信息（load_xlsx 优先走这条）
- pandas  ：降级通道，仅在 openpyxl 缺失时可达
"""

HAS_OPENPYXL = False
openpyxl = None
try:
    import openpyxl

    HAS_OPENPYXL = True
except ImportError:
    pass

HAS_PANDAS = False
pd = None
try:
    import pandas as pd

    HAS_PANDAS = True
except ImportError:
    pass
