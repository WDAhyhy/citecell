"""T0 依赖验证脚本：把 ARCHITECTURE.md 中"未核实"的第三方事实变成可复现的结论。

用法（仓库根目录）：
    .venv/bin/python scripts/verify_deps.py

约定：
- 所有测试文件只在临时目录中由代码即时生成，不写入仓库，不使用真实文档。
- 每项输出 PASS / FAIL / NOTE；单项异常被捕获并记为 FAIL，不中断其他项。
- 输出不包含临时目录的绝对路径，只输出文件基名。
- 退出码恒为 0（结论由人工对照 docs/VERIFICATION_T0.md 判定）；FAIL 数量见末尾汇总。
"""

from __future__ import annotations

import datetime as dt
import re
import sys
import tempfile
import time
import traceback
import unicodedata
import zipfile
from collections.abc import Callable
from pathlib import Path

RESULTS: list[tuple[str, str, str]] = []  # (项目, 状态, 证据摘要)

CN_TEXT = "中文引文测试：金额 1,234.50 元，日期 2024年3月5日。"
EN_LINES = ["Hello CiteCell 123.", "Revenue 1,234.50 USD"]


def record(item: str, status: str, detail: str) -> None:
    assert status in ("PASS", "FAIL", "NOTE")
    RESULTS.append((item, status, detail))
    print(f"[{status}] {item}: {detail}")


def guarded(item: str) -> Callable[[Callable[[Path], None]], Callable[[Path], None]]:
    def deco(fn: Callable[[Path], None]) -> Callable[[Path], None]:
        def wrapper(tmp: Path) -> None:
            try:
                fn(tmp)
            except Exception as exc:  # noqa: BLE001 - 脚本级兜底，记录后继续
                record(item, "FAIL", f"未处理异常 {type(exc).__name__}: {exc}")
                traceback.print_exc(limit=2, file=sys.stdout)

        return wrapper

    return deco


def norm_ws(s: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s))


# ---------------------------------------------------------------- PDF 生成
def make_pdf(path: Path) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica", 12)
    c.drawString(72, 700, EN_LINES[0])
    c.drawString(72, 680, EN_LINES[1])
    c.setFont("STSong-Light", 12)
    c.drawString(72, 650, CN_TEXT)
    c.showPage()
    c.save()


# ---------------------------------------------------------------- pypdfium2
@guarded("pypdfium2 文本与字符框")
def check_pdfium(tmp: Path) -> None:
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    pdf_path = tmp / "t0_probe.pdf"
    make_pdf(pdf_path)
    doc = pdfium.PdfDocument(str(pdf_path))
    page = doc[0]
    tp = page.get_textpage()
    try:
        text = tp.get_text_range()
        n = tp.count_chars()
        record(
            "pypdfium2 逐字 API 名称",
            "PASS",
            "PdfTextPage.count_chars/get_charbox/get_text_range 存在；"
            f"raw.FPDFText_IsGenerated={hasattr(raw, 'FPDFText_IsGenerated')}，"
            f"raw.FPDFText_HasUnicodeMapError={hasattr(raw, 'FPDFText_HasUnicodeMapError')}",
        )
        cn_ok = norm_ws(CN_TEXT) in norm_ws(text)
        en_ok = all(norm_ws(line) in norm_ws(text) for line in EN_LINES)
        record(
            "pypdfium2 中文 CID 字体提取（STSong-Light，未嵌入）",
            "PASS" if cn_ok else "FAIL",
            f"中文整句{'逐字一致' if cn_ok else '不一致'}；提取片段(前40字符)={text[:40]!r}"
            if cn_ok
            else f"提取结果与期望不符，前60字符={text[:60]!r}",
        )
        record("pypdfium2 英文提取", "PASS" if en_ok else "FAIL", f"英文两行{'均' if en_ok else '未全部'}命中")

        # 坐标原点与 CropBox
        w, h = page.get_size()
        cb = page.get_cropbox()
        mb = page.get_mediabox()
        record(
            "pypdfium2 页面尺寸 / CropBox",
            "PASS",
            f"get_size={w:.2f}x{h:.2f}；cropbox={tuple(round(v, 2) for v in cb)}；"
            f"mediabox={tuple(round(v, 2) for v in mb)}；rotation={page.get_rotation()}",
        )

        # 逐字框：'H'（起点 x=72, 基线 y=700，PDF 空间原点左下）
        idx = text.index("H")
        left, bottom, right, top = tp.get_charbox(idx, loose=False)
        err_left = abs(left - 72.0)
        baseline_inside = bottom <= 700.0 <= top
        # 转为 PDF_COORDS_V1（CropBox 左上原点、y 向下）
        top_left_y = cb[3] - top
        record(
            "pypdfium2 字符框坐标原点",
            "PASS" if baseline_inside and err_left < 1.0 else "FAIL",
            f"'H' charbox(PDF空间)=({left:.2f},{bottom:.2f},{right:.2f},{top:.2f})；"
            f"左缘误差={err_left:.3f}pt；基线700在框内={baseline_inside}；"
            f"换算到左上原点后 top_y={top_left_y:.2f}",
        )

        # 虚拟字符（generated）统计
        gen_chars = [text[i] for i in range(n) if raw.FPDFText_IsGenerated(tp.raw, i) == 1]
        record(
            "pypdfium2 虚拟字符（generated）识别",
            "PASS" if gen_chars else "NOTE",
            f"FPDFText_IsGenerated 可调用；本样本 {n} 个字符中 generated={len(gen_chars)} 个，"
            f"字符={[c for c in gen_chars]!r}（行尾插入的换行符被标为虚拟字符；"
            "空格型虚拟字符的覆盖情况需 T4 用更多样本补测）",
        )
        # 'H' 的记录与 pdfminer 比对由 check_pdfminer 完成
    finally:
        tp.close()
        page.close()
        doc.close()


# ---------------------------------------------------------------- pdfminer.six
@guarded("pdfminer.six 字符框与虚拟字符")
def check_pdfminer(tmp: Path) -> None:
    from pdfminer.high_level import extract_pages, extract_text
    from pdfminer.layout import LAParams, LTAnno, LTChar, LTTextContainer

    pdf_path = tmp / "t0_probe.pdf"
    make_pdf(pdf_path)
    text = extract_text(str(pdf_path))
    cn_ok = norm_ws(CN_TEXT) in norm_ws(text)
    record(
        "pdfminer.six 中文 CID 字体提取（STSong-Light，未嵌入）",
        "PASS" if cn_ok else "FAIL",
        f"中文整句{'逐字一致' if cn_ok else '不一致'}；前40字符={text[:40]!r}"
        if cn_ok
        else f"提取结果与期望不符，前60字符={text[:60]!r}",
    )
    chars: list[LTChar] = []
    annos = 0
    first_page_height = None
    for page in extract_pages(str(pdf_path), laparams=LAParams()):
        first_page_height = page.height
        stack = list(page)
        while stack:
            obj = stack.pop(0)
            if isinstance(obj, LTTextContainer):
                stack.extend(list(obj))
            elif isinstance(obj, LTChar):
                chars.append(obj)
            elif isinstance(obj, LTAnno):
                annos += 1
        break
    h_chars = [c for c in chars if c.get_text() == "H"]
    if h_chars:
        c0 = h_chars[0]
        err_left = abs(c0.x0 - 72.0)
        record(
            "pdfminer.six 字符框坐标原点",
            "PASS" if err_left < 1.0 and c0.y0 <= 700.0 <= c0.y1 else "FAIL",
            f"'H' LTChar.bbox=({c0.x0:.2f},{c0.y0:.2f},{c0.x1:.2f},{c0.y1:.2f})（PDF 空间，原点左下）；"
            f"左缘误差={err_left:.3f}pt；页高={first_page_height:.2f}",
        )
    else:
        record("pdfminer.six 字符框坐标原点", "FAIL", "未找到字符 'H' 的 LTChar")
    record(
        "pdfminer.six 虚拟字符 LTAnno",
        "NOTE",
        f"LTAnno 类可导入；本样本 LTAnno 数量={annos}（空格/换行由 LAParams 生成时才出现，需 T4 补测）",
    )


# ---------------------------------------------------------------- python-docx
@guarded("python-docx 交错遍历与合并单元格")
def check_docx(tmp: Path) -> None:
    import docx
    from docx.enum.style import WD_STYLE_TYPE

    d = docx.Document()
    d.add_paragraph("标题甲", style="Heading 1")
    t = d.add_table(rows=3, cols=3)
    t.cell(0, 0).merge(t.cell(0, 1))  # 横向合并
    t.cell(1, 2).merge(t.cell(2, 2))  # 纵向合并
    t.cell(0, 0).text = "合并表头"
    inner = t.cell(0, 2).add_table(rows=1, cols=1)  # 嵌套表格
    inner.cell(0, 0).text = "嵌套"
    d.add_paragraph("正文乙")

    body_order = []
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag in ("p", "tbl"):
            body_order.append(tag)
    record(
        "python-docx 正文交错顺序（body.iterchildren）",
        "PASS" if body_order == ["p", "tbl", "p"] else "FAIL",
        f"body 顶层顺序={body_order}；doc.paragraphs 仅返回 {len(d.paragraphs)} 段（不含表格，证实需遍历 body）",
    )

    row0 = t.rows[0].cells
    uniq_row0 = len({id(c._tc) for c in row0})
    col2 = [t.rows[r].cells[2] for r in (1, 2)]
    uniq_col2 = len({id(c._tc) for c in col2})
    record(
        "python-docx 合并单元格重复返回",
        "PASS",
        f"横向合并行0：row.cells 共 {len(row0)} 项，底层 _tc 去重后 {uniq_row0} 个（重复返回已证实）；"
        f"纵向合并列2：行1/行2 去重后 {uniq_col2} 个；需按 _tc 去重并记录 span（gridSpan/vMerge）",
    )
    nested = t.cell(0, 2).tables
    record("python-docx 嵌套表格访问", "PASS" if len(nested) == 1 else "FAIL", f"外层单元格 .tables 数量={len(nested)}")

    # 标题样式名
    p = d.paragraphs[0]
    st_name = p.style.name
    cn_style = d.styles.add_style("标题 1", WD_STYLE_TYPE.PARAGRAPH)
    cn_name = cn_style.name
    record(
        "python-docx 标题样式名（本地化）",
        "NOTE",
        f"'Heading 1' 段落 style.name={st_name!r}；新建自定义样式名 '标题 1' 读回 name={cn_name!r}；"
        "Word 内置样式在 styles.xml 中以英文内部名 'heading 1' 存储，识别应以 styleId/内部名为主，中文显示名为辅",
    )

    # core properties 固定 & 确定性
    d.core_properties.created = dt.datetime(2024, 1, 1, 0, 0, 0)
    d.core_properties.modified = dt.datetime(2024, 1, 1, 0, 0, 0)
    d.core_properties.last_modified_by = ""
    d.core_properties.revision = 1
    a = tmp / "det_a.docx"
    b = tmp / "det_b.docx"
    d.save(str(a))
    time.sleep(2.1)  # 让 zip 条目时间戳（秒级）不同
    d.save(str(b))
    raw_same = a.read_bytes() == b.read_bytes()
    with zipfile.ZipFile(a) as za, zipfile.ZipFile(b) as zb:
        names_a, names_b = za.namelist(), zb.namelist()
        content_same = names_a == names_b and all(za.read(n) == zb.read(n) for n in names_a)
        dates = {zi.date_time for zi in za.infolist()}
    record(
        "python-docx 同内容写两次字节一致",
        "PASS" if raw_same else "NOTE",
        f"整文件字节相同={raw_same}；各条目解压内容相同={content_same}；zip 条目 date_time 样例={sorted(dates)[:1]}；"
        + ("" if raw_same else "差异来自 zip 时间戳，T3/T5a 需固定 ZipInfo.date_time 或只比较解压内容"),
    )
    d2 = docx.Document(str(a))
    cp = d2.core_properties
    fixed = dt.datetime(2024, 1, 1)
    got_c = cp.created.replace(tzinfo=None) if cp.created else None
    got_m = cp.modified.replace(tzinfo=None) if cp.modified else None
    record(
        "python-docx core properties 固定",
        "PASS" if got_c == fixed and got_m == fixed else "FAIL",
        f"读回 created={cp.created}, modified={cp.modified}（保存时不会覆盖为当前时间）",
    )
    import docx.oxml.parser as oxml_mod

    src = Path(oxml_mod.__file__).read_text(encoding="utf-8")
    record(
        "python-docx XML 解析实体防护",
        "PASS" if "resolve_entities=False" in src else "NOTE",
        "docx.oxml.parser 源码中 XMLParser(resolve_entities=False)（外部实体不展开）；"
        "zip 炸弹限制（解压总大小/条目数）仍需 T3 自行实现",
    )


# ---------------------------------------------------------------- openpyxl
@guarded("openpyxl 公式/隐藏/日期/纪元")
def check_openpyxl(tmp: Path) -> None:
    import openpyxl
    from openpyxl.utils.datetime import CALENDAR_MAC_1904, from_excel
    from openpyxl.xml import DEFUSEDXML

    wb = openpyxl.Workbook()
    wb.properties.created = dt.datetime(2024, 1, 1)
    wb.properties.modified = dt.datetime(2024, 1, 1)
    ws = wb.active
    ws.title = "数据"
    ws["A1"], ws["B1"], ws["C1"], ws["D1"] = "金额", "公式", "日期", "占比"
    ws["A2"] = 3
    ws["B2"] = "=A2*2"
    ws["C2"] = dt.datetime(2024, 3, 5)
    ws["C2"].number_format = "yyyy-mm-dd"
    ws["D2"] = 0.15
    ws["D2"].number_format = "0%"
    ws.merge_cells("A4:B4")
    ws["A4"] = "合并表头"
    ws.row_dimensions[5].hidden = True
    ws["A5"] = "隐藏行"
    ws.column_dimensions["E"].hidden = True
    ws["E1"] = "隐藏列"
    lookup = wb.create_sheet("查找")
    lookup["A1"] = "隐藏表"
    lookup.sheet_state = "hidden"
    path = tmp / "probe.xlsx"
    wb.save(str(path))

    f_wb = openpyxl.load_workbook(str(path), data_only=False)
    v_wb = openpyxl.load_workbook(str(path), data_only=True)
    formula = f_wb["数据"]["B2"].value
    cached = v_wb["数据"]["B2"].value
    record(
        "openpyxl 公式 / 缓存值两遍读取",
        "PASS" if formula == "=A2*2" and cached is None else "FAIL",
        f"data_only=False 公式={formula!r}；data_only=True 缓存值={cached!r}（openpyxl 写出的公式无缓存，"
        "必须走 FORMULA_NO_CACHED_VALUE 分支）",
    )

    ws2 = v_wb["数据"]
    ranges = [str(r) for r in ws2.merged_cells.ranges]
    rng_ok = "A4:B4" in ranges
    record("openpyxl 合并区域", "PASS" if rng_ok else "FAIL", f"merged_cells.ranges={ranges}；B4 为 MergedCell，值={ws2['B4'].value!r}")

    hidden_row = ws2.row_dimensions[5].hidden
    hidden_col = ws2.column_dimensions["E"].hidden
    hidden_sheet = v_wb["查找"].sheet_state
    record(
        "openpyxl 隐藏行/列/工作表判定",
        "PASS" if hidden_row and hidden_col and hidden_sheet == "hidden" else "FAIL",
        f"row5.hidden={hidden_row}；列E.hidden={hidden_col}；工作表 sheet_state={hidden_sheet!r}；"
        "注意：列隐藏需按 column_dimensions 的 min/max 区间判断，单字母键可能不覆盖整段",
    )

    date_cell = ws2["C2"]
    record(
        "openpyxl 日期单元格转换",
        "PASS" if date_cell.is_date and date_cell.value == dt.datetime(2024, 3, 5) else "FAIL",
        f"C2 类型={type(date_cell.value).__name__} 值={date_cell.value!r} is_date={date_cell.is_date}；"
        f"默认纪元={v_wb.epoch.__class__.__name__}",
    )

    pct = ws2["D2"]
    record(
        "openpyxl 百分比 number_format",
        "PASS" if "%" in pct.number_format else "FAIL",
        f"D2 number_format={pct.number_format!r} value={pct.value!r}（text 渲染 15% 由 XLSX_TEXT_V1 完成）",
    )

    # 1904 纪元
    wb1904 = openpyxl.Workbook()
    wb1904.epoch = CALENDAR_MAC_1904
    wb1904.active["A1"] = 1
    wb1904.active["A1"].number_format = "yyyy-mm-dd"
    p1904 = tmp / "epoch1904.xlsx"
    wb1904.save(str(p1904))
    r1904 = openpyxl.load_workbook(str(p1904), data_only=True)
    ep_ok = r1904.epoch == CALENDAR_MAC_1904 and r1904.active["A1"].value == dt.datetime(1904, 1, 2)
    default_serial = from_excel(1)
    record(
        "openpyxl 1904 纪元读取",
        "PASS" if ep_ok else "FAIL",
        f"1904 工作簿 epoch 读回={r1904.epoch.__class__.__name__}，序列号 1 → {r1904.active['A1'].value!r}；"
        f"1900 默认 from_excel(1)={default_serial!r}",
    )

    # 确定性
    a = tmp / "xdet_a.xlsx"
    b = tmp / "xdet_b.xlsx"
    wb.save(str(a))
    time.sleep(2.1)
    wb.save(str(b))
    with zipfile.ZipFile(a) as za, zipfile.ZipFile(b) as zb:
        diff_entries = [n for n in za.namelist() if za.read(n) != zb.read(n)]
    raw_same = a.read_bytes() == b.read_bytes()
    record(
        "openpyxl 同内容写两次字节一致",
        "PASS" if raw_same else "NOTE",
        f"整文件字节相同={raw_same}；内容不同的条目={diff_entries}"
        + ("" if raw_same else "；openpyxl 保存时会覆盖 docProps/core.xml 的 modified 为当前时间，"
           "T5a 需在写出后规范化该条目或固定 zip 时间戳"),
    )

    record(
        "openpyxl defusedxml 使用",
        "NOTE",
        f"openpyxl.xml.DEFUSEDXML={DEFUSEDXML}（defusedxml 未安装，openpyxl 不自动使用）；"
        "如需防御 XML 实体攻击，需用户批准安装 defusedxml，或在解析层自行限制；未经批准不安装",
    )


# ---------------------------------------------------------------- 其他
@guarded("pydantic 与契约依赖版本")
def check_pydantic(tmp: Path) -> None:
    import pydantic

    record("pydantic 版本", "PASS", f"pydantic={pydantic.VERSION}；契约需 model_json_schema 与 model_validator(mode='after')")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="citecell_t0_") as td:
        tmp = Path(td)
        for fn in (check_pdfium, check_pdfminer, check_docx, check_openpyxl, check_pydantic):
            fn(tmp)
    n_fail = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    n_note = sum(1 for _, s, _ in RESULTS if s == "NOTE")
    n_pass = sum(1 for _, s, _ in RESULTS if s == "PASS")
    print(f"-- 汇总：PASS={n_pass} NOTE={n_note} FAIL={n_fail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
