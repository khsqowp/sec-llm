#!/usr/bin/env python3
"""
doc2md.py — 문서(Word/PDF/Excel/이미지/TXT/MD) → 마크다운 변환기
Word/Excel 내 임베드 이미지 포함 전체 추출. Vision 모델(minicpm-v:8b) 사용.
"""

import sys
import os
import argparse
import json
import io
import base64
from pathlib import Path

# ── 의존성 체크 ────────────────────────────────────────────────
try:
    from markitdown import MarkItDown
    MARKITDOWN_OK = True
except ImportError:
    MARKITDOWN_OK = False
    print("WARNING: markitdown 없음. pip install markitdown[all]")

try:
    import pdfplumber
    PDFPLUMBER_OK = True
except ImportError:
    PDFPLUMBER_OK = False

try:
    import pandas as pd
    PANDAS_OK = True
except ImportError:
    PANDAS_OK = False

try:
    import ollama as ollama_lib
    OLLAMA_OK = True
except ImportError:
    OLLAMA_OK = False

try:
    from pdf2image import convert_from_path
    from PIL import Image
    IMAGE_OK = True
except ImportError:
    IMAGE_OK = False

try:
    from docx import Document as DocxDocument
    from docx.oxml.ns import qn
    DOCX_OK = True
except ImportError:
    DOCX_OK = False

try:
    import openpyxl
    OPENPYXL_OK = True
except ImportError:
    OPENPYXL_OK = False

VISION_MODEL = os.getenv("VISION_MODEL", "minicpm-v:8b")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
TEXT_THRESHOLD = 100  # 페이지당 문자 수 미만이면 스캔 판단


# ── Vision 분석 ────────────────────────────────────────────────
def vision_analyze(image: "Image.Image", prompt: str = None) -> str:
    if not OLLAMA_OK or not IMAGE_OK:
        return "<!-- vision 불가: ollama/PIL 없음 -->"
    if prompt is None:
        prompt = (
            "이 문서 이미지의 모든 내용을 마크다운 형식으로 정확하게 추출하라. "
            "표는 마크다운 테이블로, 제목은 # 으로, 목록은 - 로 변환하라. "
            "텍스트 하나도 빠짐없이 추출하라. 설명이나 주석 없이 내용만 출력하라."
        )
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    img_b64 = base64.b64encode(buf.getvalue()).decode()
    try:
        client = ollama_lib.Client(host=OLLAMA_HOST)
        resp = client.chat(
            model=VISION_MODEL,
            messages=[{"role": "user", "content": prompt, "images": [img_b64]}],
            options={"num_ctx": 8192},
        )
        return resp["message"]["content"].strip()
    except Exception as e:
        return f"<!-- vision error: {e} -->"


def bytes_to_image(data: bytes) -> "Image.Image":
    return Image.open(io.BytesIO(data)).convert("RGB")


# ── 표 → 마크다운 ──────────────────────────────────────────────
def table_to_md(table: list) -> str:
    if not table:
        return ""
    rows = []
    for i, row in enumerate(table):
        cells = [str(c).strip() if c is not None else "" for c in row]
        rows.append("| " + " | ".join(cells) + " |")
        if i == 0:
            rows.append("| " + " | ".join(["---"] * len(cells)) + " |")
    return "\n".join(rows)


# ── DOCX 처리 (임베드 이미지 포함) ────────────────────────────
def extract_docx(path: Path) -> str:
    if not DOCX_OK:
        if MARKITDOWN_OK:
            return MarkItDown().convert(str(path)).text_content
        return "<!-- python-docx 없음 -->"

    doc = DocxDocument(str(path))
    parts = []
    img_counter = [0]

    def process_paragraph(para):
        text = para.text.strip()
        result = []

        # 헤딩 스타일 감지
        style = para.style.name if para.style else ""
        if "Heading 1" in style:
            if text:
                result.append(f"# {text}")
        elif "Heading 2" in style:
            if text:
                result.append(f"## {text}")
        elif "Heading 3" in style:
            if text:
                result.append(f"### {text}")
        elif text:
            result.append(text)

        # 인라인 이미지 (run 내 drawing/inline 요소)
        for run in para.runs:
            for elem in run._r:
                tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                if tag in ("drawing", "pict"):
                    # 이미지 blip 찾기
                    for blip in elem.iter():
                        btag = blip.tag.split("}")[-1] if "}" in blip.tag else blip.tag
                        if btag == "blip":
                            rId = blip.get(
                                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
                            )
                            if rId and rId in doc.part.rels:
                                img_part = doc.part.rels[rId].target_part
                                img_counter[0] += 1
                                n = img_counter[0]
                                print(f"    이미지 {n} → vision 분석")
                                img = bytes_to_image(img_part.blob)
                                vision_text = vision_analyze(img)
                                result.append(f"\n<!-- 임베드 이미지 {n} -->\n{vision_text}\n")
                            break
        return result

    def process_table(tbl):
        rows = []
        for i, row in enumerate(tbl.rows):
            cells = [cell.text.strip() for cell in row.cells]
            rows.append("| " + " | ".join(cells) + " |")
            if i == 0:
                rows.append("| " + " | ".join(["---"] * len(cells)) + " |")
        return "\n".join(rows)

    # 문서 순서대로 단락/표 처리 (XML 순서 유지)
    body = doc.element.body
    for child in body:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "p":
            from docx.text.paragraph import Paragraph
            para = Paragraph(child, doc)
            lines = process_paragraph(para)
            parts.extend(lines)
        elif tag == "tbl":
            from docx.table import Table
            tbl = Table(child, doc)
            parts.append(process_table(tbl))

    # 문서 내 독립 이미지 파트 (인라인 외 floating 이미지)
    rels = doc.part.rels
    found_float = False
    for rId, rel in rels.items():
        if "image" in rel.reltype:
            # 이미 처리된 이미지인지 확인 어려움 → 전체 처리 후 중복 방지는 blip embed로
            pass

    return "\n\n".join(p for p in parts if p.strip())


# ── XLSX 처리 (임베드 이미지 포함) ────────────────────────────
def extract_excel(path: Path) -> str:
    sections = []

    if not PANDAS_OK:
        if MARKITDOWN_OK:
            return MarkItDown().convert(str(path)).text_content
        return "<!-- pandas 없음 -->"

    try:
        xl = pd.ExcelFile(str(path))
        for sheet_name in xl.sheet_names:
            sections.append(f"## 시트: {sheet_name}\n")
            df = pd.read_excel(str(path), sheet_name=sheet_name, header=None)
            if not df.empty:
                # 첫 행 헤더 자동 감지
                first_row = df.iloc[0]
                if all(isinstance(v, str) for v in first_row if pd.notna(v)):
                    df.columns = df.iloc[0]
                    df = df[1:].reset_index(drop=True)
                sections.append(df.fillna("").to_markdown(index=False))
    except Exception as e:
        sections.append(f"<!-- excel 텍스트 오류: {e} -->")

    # 임베드 이미지 추출
    if OPENPYXL_OK and IMAGE_OK:
        try:
            wb = openpyxl.load_workbook(str(path))
            img_counter = 0
            for ws in wb.worksheets:
                sheet_imgs = []
                for img_obj in ws._images:
                    try:
                        img_data = img_obj._data()
                        if img_data:
                            img_counter += 1
                            print(f"    시트 '{ws.title}' 이미지 {img_counter} → vision 분석")
                            pil_img = bytes_to_image(img_data)
                            vision_text = vision_analyze(pil_img)
                            sheet_imgs.append(
                                f"\n<!-- 시트 '{ws.title}' 임베드 이미지 {img_counter} -->\n{vision_text}\n"
                            )
                    except Exception as e:
                        sheet_imgs.append(f"<!-- 이미지 추출 오류: {e} -->")
                if sheet_imgs:
                    sections.append(f"\n### 시트 '{ws.title}' 이미지 내용\n")
                    sections.extend(sheet_imgs)
        except Exception as e:
            sections.append(f"<!-- xlsx 이미지 추출 오류: {e} -->")

    return "\n\n".join(s for s in sections if str(s).strip())


# ── PDF 처리 ───────────────────────────────────────────────────
def extract_pdf(path: Path) -> str:
    sections = []
    text_by_page = []
    tables_by_page = []

    if PDFPLUMBER_OK:
        try:
            with pdfplumber.open(str(path)) as pdf:
                for i, page in enumerate(pdf.pages):
                    text = page.extract_text() or ""
                    text_by_page.append((i, text))
                    tables = page.extract_tables()
                    if tables:
                        tables_by_page.append((i, tables))
        except Exception:
            pass

    images = []
    if IMAGE_OK:
        try:
            images = convert_from_path(str(path), dpi=200)
        except Exception:
            pass

    for i, (page_idx, text) in enumerate(text_by_page):
        is_scanned = len(text.strip()) < TEXT_THRESHOLD
        page_parts = [f"<!-- page {i+1} -->"]

        if is_scanned and images and i < len(images):
            print(f"  페이지 {i+1}: 스캔 감지 → vision 분석")
            page_parts.append(vision_analyze(images[i]))
        else:
            if text.strip():
                page_parts.append(text)
            # 페이지 내 이미지도 vision으로 보충
            if images and i < len(images) and text.strip():
                # 텍스트 있어도 이미지 내용 추가 (차트/표 이미지 대비)
                pass

        for tidx, tables in tables_by_page:
            if tidx == page_idx:
                for tbl in tables:
                    page_parts.append(table_to_md(tbl))

        sections.append("\n".join(page_parts))

    if not sections:
        if MARKITDOWN_OK:
            return MarkItDown().convert(str(path)).text_content
        return "<!-- PDF 추출 실패 -->"

    return "\n\n".join(sections)


# ── 이미지 처리 ────────────────────────────────────────────────
def extract_image(path: Path) -> str:
    if not IMAGE_OK:
        return "<!-- pillow 없음 -->"
    try:
        img = Image.open(str(path)).convert("RGB")
        return vision_analyze(img)
    except Exception as e:
        return f"<!-- 이미지 오류: {e} -->"


# ── TXT / MD ───────────────────────────────────────────────────
def extract_text(path: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp949", "euc-kr"):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return path.read_bytes().decode("utf-8", errors="replace")


# ── 품질 점수 ──────────────────────────────────────────────────
def quality_score(text: str) -> int:
    if not text.strip():
        return 0
    lines = [l for l in text.splitlines() if l.strip()]
    non_comment = [l for l in lines if not l.startswith("<!--")]
    if not lines:
        return 0
    ratio = len(non_comment) / len(lines)
    chars = len(text.strip())
    if chars > 5000:
        base = 95
    elif chars > 1000:
        base = 85
    elif chars > 200:
        base = 70
    else:
        base = 40
    return min(100, int(base * ratio))


# ── 메인 변환 ──────────────────────────────────────────────────
def convert_file(src: Path, out_dir: Path, label: str = "") -> dict:
    suffix = src.suffix.lower()
    print(f"\n[{src.name}] 처리 중... (label={label or '-'})")

    header = f"---\nsource: {src.name}\nlabel: {label}\n---\n\n"

    try:
        if suffix == ".pdf":
            content = extract_pdf(src)
        elif suffix in (".xlsx", ".xls"):
            content = extract_excel(src)
        elif suffix == ".csv":
            content = extract_text(src)
        elif suffix in (".docx", ".doc"):
            content = extract_docx(src)
        elif suffix in (".pptx", ".ppt"):
            if MARKITDOWN_OK:
                content = MarkItDown().convert(str(src)).text_content
            else:
                content = "<!-- markitdown 없음 -->"
        elif suffix in (".txt", ".md", ".text"):
            content = extract_text(src)
        elif suffix in (".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".webp"):
            content = extract_image(src)
        elif MARKITDOWN_OK:
            content = MarkItDown().convert(str(src)).text_content
        else:
            content = "<!-- 지원하지 않는 형식 -->"
    except Exception as e:
        content = f"<!-- 변환 실패: {e} -->"

    score = quality_score(content)
    out_path = out_dir / (src.stem + ".md")
    out_path.write_text(header + content, encoding="utf-8")
    print(f"  ✓ {out_path.name} | 품질: {score}% | {len(content):,}자")
    return {"file": src.name, "output": str(out_path), "quality": score, "chars": len(content)}


# ── CLI ────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="문서 → 마크다운 변환기 (임베드 이미지 포함)")
    parser.add_argument("input", help="파일 또는 폴더 경로")
    parser.add_argument("--out", "-o", default=None, help="출력 폴더")
    parser.add_argument("--label", "-l", default="", help="레이블 (guideline / report 등)")
    parser.add_argument("--vault", "-v", default=None, help="완료 후 Obsidian vault 경로로 복사")
    parser.add_argument("--vision-model", default=None, help="Vision 모델 (기본: minicpm-v:8b)")
    args = parser.parse_args()

    global VISION_MODEL
    if args.vision_model:
        VISION_MODEL = args.vision_model

    src = Path(args.input)
    out_dir = Path(args.out) if args.out else (
        src.parent / "_md" if src.is_dir() else src.parent / "_md"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    SUPPORTED = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv",
                 ".pptx", ".ppt", ".rtf", ".txt", ".md", ".text",
                 ".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".webp"}

    files = []
    if src.is_dir():
        files = [f for f in src.rglob("*") if f.suffix.lower() in SUPPORTED]
    elif src.is_file():
        files = [src]
    else:
        print(f"ERROR: {src} 없음")
        sys.exit(1)

    if not files:
        print("변환할 파일 없음")
        sys.exit(0)

    print(f"총 {len(files)}개 파일 처리 시작")
    results = []
    for f in sorted(files):
        r = convert_file(f, out_dir, label=args.label)
        results.append(r)

    # vault 복사
    if args.vault:
        vault_dir = Path(args.vault)
        vault_dir.mkdir(parents=True, exist_ok=True)
        for r in results:
            p = Path(r["output"])
            dest = vault_dir / p.name
            dest.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"  vault: {dest}")

    # 요약
    total = len(results)
    avg_q = sum(r["quality"] for r in results) / total if total else 0
    low = [r for r in results if r["quality"] < 80]
    print(f"\n═══ 완료 ═══")
    print(f"총 {total}개 | 평균 품질: {avg_q:.0f}%")
    if low:
        print("품질 80% 미만:")
        for r in low:
            print(f"  - {r['file']}: {r['quality']}%")

    report_path = out_dir / "_report.json"
    report_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"리포트: {report_path}")


if __name__ == "__main__":
    main()
