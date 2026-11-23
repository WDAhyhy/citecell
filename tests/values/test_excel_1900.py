"""T0 遗留缺口：Excel 1900 日期系统的 1900 闰年错误（VERIFICATION_T0.md 未测）。

结论（本测试记录的证据）：
- openpyxl 把序列号 60（不存在的 1900-02-29）映射为 1900-02-28；序列号 61 → 1900-03-01。
- T2 不把 Excel 日期序列号当作值的表面形式（ARCH §6.3 只识别 ISO 字符串，
  XLSX 日期单元格由 T3 以 ISO 渲染后再交给 T2）。因此 T2 不实现序列号换算，
  该 1900 错误对 T2 无影响；若将来支持序列号，必须按此测试的结论处理 60 号。
"""

import datetime as dt

from openpyxl.utils.datetime import from_excel

from citecell.values.dates import parse_date_text


def test_openpyxl_serial_60_is_mapped_to_feb_28():
    assert from_excel(59) == dt.datetime(1900, 2, 28)
    assert from_excel(60) == dt.datetime(1900, 2, 28)  # 非法日期 1900-02-29 被折叠
    assert from_excel(61) == dt.datetime(1900, 3, 1)


def test_t2_rejects_nonexistent_1900_leap_day_as_text():
    # 文本形式的 1900-02-29 必须被拒绝（公历 1900 不是闰年）
    import pytest

    from citecell.interfaces import ValueParseError

    with pytest.raises(ValueParseError):
        parse_date_text("1900-02-29")
    with pytest.raises(ValueParseError):
        parse_date_text("1900年2月29日")
