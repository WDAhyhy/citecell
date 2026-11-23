"""合成生成器接入 PDF：混合格式生成、PDF 金标准校验、哈希与字节确定性。"""

import hashlib
from pathlib import Path

from citecell.models import GenerationConfig, GoldDocument, PdfLocator
from citecell.synth.generator import generate
from citecell.synth.keysets import load_keyset, load_preset


def _config(formats, n_docs=6, seed=1) -> GenerationConfig:
    return GenerationConfig(
        seed=seed,
        n_docs=n_docs,
        formats=list(formats),
        keyset=load_keyset(),
        difficulty=load_preset("medium"),
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_mixed_docx_xlsx_pdf_generation_gold_valid(tmp_path):
    out = tmp_path / "mix"
    samples = list(generate(_config(("docx", "xlsx", "pdf"), n_docs=6), out))
    assert len(samples) == 6
    formats_seen = {s.relative_path.rsplit(".", 1)[1] for s in samples}
    assert formats_seen == {"docx", "xlsx", "pdf"}
    for s in samples:
        path = out / s.relative_path
        assert path.is_file(), s.relative_path
        gold = GoldDocument.model_validate_json(s.gold.model_dump_json())
        assert gold.doc_sha256 == _sha(path), s.relative_path
        if s.relative_path.endswith(".pdf"):
            assert gold.format.value == "pdf"
            assert path.read_bytes().startswith(b"%PDF")
            present = [m for r in gold.records for m in r.mentions]
            assert present, "PDF 样本应至少有一处出现"
            for m in present:
                assert isinstance(m.locator, PdfLocator)
                assert m.char_start is None and m.char_end is None
                assert m.locator.page_number >= 1
                assert m.locator.regions and m.surface_text


def test_pdf_same_seed_twice_identical_bytes(tmp_path):
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    list(generate(_config(("pdf",), n_docs=3), out_a))
    list(generate(_config(("pdf",), n_docs=3), out_b))
    pdfs_a = sorted(out_a.glob("*.pdf"))
    assert len(pdfs_a) == 3
    for pa in pdfs_a:
        pb = out_b / pa.name
        assert _sha(pa) == _sha(pb), pa.name
    assert (out_a / "gold.json").read_bytes() == (out_b / "gold.json").read_bytes()
    print("\n[pdf determinism] " + ", ".join(f"{p.name}={_sha(p)[:12]}" for p in pdfs_a))


def test_mixed_same_seed_identical_for_all_formats(tmp_path):
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    list(generate(_config(("docx", "xlsx", "pdf"), n_docs=6), out_a))
    list(generate(_config(("docx", "xlsx", "pdf"), n_docs=6), out_b))
    for pa in sorted(out_a.iterdir()):
        assert _sha(pa) == _sha(out_b / pa.name), pa.name
