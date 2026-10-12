"""
파일 형식별 텍스트 추출 모듈
지원: PDF, DOCX, XLSX, PPTX, HWP, TXT, MD, 코드 파일, 이미지, SQLite, SQL/TSV
"""
import logging
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

CODE_LANG_MAP = {
    ".py": "python", ".java": "java", ".js": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript",
    ".c": "c", ".cpp": "cpp", ".h": "c", ".go": "go",
    ".rs": "rust", ".sh": "bash", ".bash": "bash", ".sql": "sql",
}


def parse_file(
    file_path: Path,
    ollama_url: str = "http://host.docker.internal:11434",
    vision_model: str = "llava:7b",
) -> Tuple[str, str]:
    """
    파일을 파싱하여 (텍스트, 파일유형) 반환.
    파일유형: 'document' | 'code' | 'image' | 'data' | 'unknown'
    문서(PDF/DOCX/XLSX/PPTX): markitdown 먼저 시도, 실패 시 기존 파서 fallback.
    이미지: Ollama vision 먼저 시도, 실패 시 pytesseract fallback.
    """
    ext = file_path.suffix.lower()

    try:
        if ext == ".pdf":
            text = _parse_with_markitdown(file_path) or _parse_pdf(file_path)
            return text, "document"
        elif ext == ".docx":
            text = _parse_with_markitdown(file_path) or _parse_docx(file_path)
            return text, "document"
        elif ext == ".xlsx":
            text = _parse_with_markitdown(file_path) or _parse_xlsx(file_path)
            return text, "document"
        elif ext == ".pptx":
            text = _parse_with_markitdown(file_path) or _parse_pptx(file_path)
            return text, "document"
        elif ext == ".hwp":
            return _parse_hwp(file_path), "document"
        elif ext == ".sqlite3" or ext == ".db":
            return _parse_sqlite(file_path), "data"
        elif ext in (".txt", ".md", ".tsv", ".csv"):
            return file_path.read_text(encoding="utf-8", errors="replace"), "document"
        elif ext in CODE_LANG_MAP:
            return file_path.read_text(encoding="utf-8", errors="replace"), "code"
        elif ext in (".jpg", ".jpeg", ".png"):
            return _parse_image(file_path, ollama_url, vision_model), "image"
        else:
            return "", "unknown"
    except Exception as e:
        logger.error(f"파싱 실패 [{file_path.name}]: {e}")
        return f"[파싱 실패: {e}]", "unknown"


def get_code_language(file_path: Path) -> str:
    return CODE_LANG_MAP.get(file_path.suffix.lower(), "text")


# ── 개별 파서 ─────────────────────────────────────────────

def _parse_with_markitdown(path: Path) -> str:
    """markitdown으로 문서를 마크다운 텍스트로 변환. 실패 시 빈 문자열 반환."""
    try:
        from markitdown import MarkItDown
        md = MarkItDown()
        result = md.convert(str(path))
        text = result.text_content.strip() if result.text_content else ""
        if text:
            logger.debug(f"markitdown 변환 성공: {path.name} ({len(text)}자)")
        return text
    except Exception as e:
        logger.debug(f"markitdown 실패, fallback 사용: {path.name} ({e})")
        return ""


def _parse_pdf(path: Path) -> str:
    import pdfplumber
    import pytesseract
    from PIL import Image

    text_parts = []
    try:
        with pdfplumber.open(path) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                # 일반 텍스트 추출
                text = page.extract_text()

                # 표 추출 → 마크다운 표 변환
                tables = page.extract_tables()
                table_md = ""
                for table in tables:
                    if table:
                        table_md += _table_to_markdown(table) + "\n"

                if text and text.strip():
                    text_parts.append(f"[페이지 {page_num}]\n{text}")
                    if table_md:
                        text_parts.append(table_md)
                else:
                    # 스캔 PDF → OCR
                    img = page.to_image(resolution=200).original
                    ocr_text = pytesseract.image_to_string(img, lang="kor+eng")
                    if ocr_text.strip():
                        text_parts.append(f"[페이지 {page_num} OCR]\n{ocr_text}")

    except Exception as e:
        logger.warning(f"pdfplumber 실패, 재시도: {e}")

    return "\n\n".join(text_parts) if text_parts else "[PDF 텍스트 추출 실패]"


def _parse_docx(path: Path) -> str:
    from docx import Document
    from docx.oxml.ns import qn

    doc = Document(path)
    parts = []

    for element in doc.element.body:
        tag = element.tag.split("}")[-1] if "}" in element.tag else element.tag
        if tag == "p":
            text = element.text_content() if hasattr(element, "text_content") else ""
            # paragraph 텍스트 직접 추출
            runs_text = "".join(r.text for r in element.findall(f".//{qn('w:t')}"))
            if runs_text.strip():
                parts.append(runs_text)
        elif tag == "tbl":
            rows = []
            for row in element.findall(f".//{qn('w:tr')}"):
                cells = []
                for cell in row.findall(f".//{qn('w:tc')}"):
                    cell_text = "".join(
                        t.text or "" for t in cell.findall(f".//{qn('w:t')}")
                    )
                    cells.append(cell_text.strip())
                if cells:
                    rows.append(cells)
            if rows:
                parts.append(_table_to_markdown(rows))

    return "\n\n".join(parts) if parts else "[DOCX 텍스트 없음]"


def _parse_xlsx(path: Path) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)
    parts = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows = []
        for row in ws.iter_rows(values_only=True):
            str_row = [str(c) if c is not None else "" for c in row]
            if any(c.strip() for c in str_row):
                rows.append(str_row)

        if rows:
            parts.append(f"### 시트: {sheet_name}")
            parts.append(_table_to_markdown(rows))

    return "\n\n".join(parts) if parts else "[XLSX 데이터 없음]"


def _parse_pptx(path: Path) -> str:
    from pptx import Presentation

    prs = Presentation(path)
    parts = []

    for slide_num, slide in enumerate(prs.slides, 1):
        slide_texts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    text = "".join(run.text for run in para.runs).strip()
                    if text:
                        slide_texts.append(text)
            # 표 처리
            if shape.has_table:
                rows = []
                for row in shape.table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    rows.append(cells)
                if rows:
                    slide_texts.append(_table_to_markdown(rows))

        if slide_texts:
            parts.append(f"[슬라이드 {slide_num}]\n" + "\n".join(slide_texts))

    return "\n\n".join(parts) if parts else "[PPTX 내용 없음]"


def _parse_hwp(path: Path) -> str:
    """LibreOffice를 통해 HWP → DOCX 변환 후 파싱"""
    if not shutil.which("libreoffice"):
        return f"[HWP 변환 실패: LibreOffice가 없습니다]"

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            result = subprocess.run(
                [
                    "libreoffice",
                    "--headless",
                    "--convert-to", "docx",
                    "--outdir", tmpdir,
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            converted_files = list(Path(tmpdir).glob("*.docx"))
            if not converted_files:
                logger.warning(f"HWP 변환 결과 없음: {result.stderr}")
                return f"[HWP 변환 실패]\n{result.stderr}"

            return _parse_docx(converted_files[0])
        except subprocess.TimeoutExpired:
            return "[HWP 변환 타임아웃]"
        except Exception as e:
            return f"[HWP 변환 오류: {e}]"


def _parse_image(
    path: Path,
    ollama_url: str = "http://host.docker.internal:11434",
    vision_model: str = "llava:7b",
) -> str:
    """이미지 분석: Ollama vision 먼저 시도, 실패 시 pytesseract OCR fallback."""
    text = _parse_image_with_vision(path, ollama_url, vision_model)
    if text:
        return text
    return _parse_image_with_ocr(path)


def _parse_image_with_vision(path: Path, ollama_url: str, vision_model: str) -> str:
    """Ollama vision API로 이미지 분석. 실패 또는 vision 모델 없으면 빈 문자열 반환."""
    import base64
    import requests

    try:
        with open(path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode()

        response = requests.post(
            f"{ollama_url}/api/generate",
            json={
                "model": vision_model,
                "prompt": (
                    "이 이미지의 내용을 상세하게 한국어로 분석해주세요. "
                    "텍스트가 있으면 모두 추출하고, 도표/차트/다이어그램이 있으면 설명해주세요."
                ),
                "images": [image_b64],
                "stream": False,
            },
            timeout=120,
        )
        response.raise_for_status()
        result = response.json().get("response", "").strip()
        if result:
            logger.debug(f"vision 분석 성공: {path.name}")
        return result
    except Exception as e:
        logger.debug(f"vision 분석 실패, OCR fallback: {path.name} ({e})")
        return ""


def _parse_image_with_ocr(path: Path) -> str:
    """pytesseract OCR로 이미지에서 텍스트 추출."""
    import pytesseract
    from PIL import Image

    try:
        img = Image.open(path)
        text = pytesseract.image_to_string(img, lang="kor+eng")
        return text.strip() if text.strip() else "[이미지에서 텍스트 없음]"
    except Exception as e:
        return f"[이미지 OCR 실패: {e}]"


def _parse_sqlite(path: Path) -> str:
    try:
        conn = sqlite3.connect(str(path))
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]

        parts = [f"# SQLite 데이터베이스: {path.name}\n테이블 수: {len(tables)}"]
        for table in tables:
            cursor.execute(f"PRAGMA table_info({table})")
            columns = [col[1] for col in cursor.fetchall()]

            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            count = cursor.fetchone()[0]

            cursor.execute(f"SELECT * FROM {table} LIMIT 20")
            rows = cursor.fetchall()

            parts.append(f"\n## 테이블: {table} (총 {count}행)")
            if rows:
                parts.append(_table_to_markdown([columns] + [list(r) for r in rows]))

        conn.close()
        return "\n\n".join(parts)
    except Exception as e:
        return f"[SQLite 파싱 실패: {e}]"


def _table_to_markdown(rows: list) -> str:
    if not rows:
        return ""
    header = rows[0]
    separator = ["---"] * len(header)
    lines = [
        "| " + " | ".join(str(c) for c in header) + " |",
        "| " + " | ".join(separator) + " |",
    ]
    for row in rows[1:]:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines)
