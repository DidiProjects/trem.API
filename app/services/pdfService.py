import io
import re
import uuid
import zipfile
from datetime import datetime
from typing import List, Literal, Optional
import pikepdf
import pymupdf as fitz
from PIL import Image
from app.utils import parse_page_ranges


class PdfService:
    
    @staticmethod
    def split(content: bytes, pages: str) -> tuple[io.BytesIO, int]:
        pdf = pikepdf.open(io.BytesIO(content))
        total_pages = len(pdf.pages)
        page_numbers = parse_page_ranges(pages, total_pages)
        
        output_pdf = pikepdf.new()
        for page_num in page_numbers:
            output_pdf.pages.append(pdf.pages[page_num - 1])
        
        output = io.BytesIO()
        output_pdf.save(output)
        output.seek(0)
        
        pdf.close()
        output_pdf.close()
        
        return output, total_pages

    @staticmethod
    def extract_pages(content: bytes) -> io.BytesIO:
        pdf = pikepdf.open(io.BytesIO(content))
        
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for i, page in enumerate(pdf.pages):
                page_pdf = pikepdf.new()
                page_pdf.pages.append(page)
                page_buffer = io.BytesIO()
                page_pdf.save(page_buffer)
                page_buffer.seek(0)
                zip_file.writestr(f"page_{i + 1}.pdf", page_buffer.read())
                page_pdf.close()
        
        zip_buffer.seek(0)
        pdf.close()
        
        return zip_buffer

    @staticmethod
    def merge(contents: List[tuple[str, bytes]]) -> io.BytesIO:
        output_pdf = pikepdf.new()
        
        for filename, content in contents:
            pdf = pikepdf.open(io.BytesIO(content))
            for page in pdf.pages:
                output_pdf.pages.append(page)
            pdf.close()
        
        output = io.BytesIO()
        output_pdf.save(output)
        output.seek(0)
        output_pdf.close()
        
        return output

    @staticmethod
    def add_password(content: bytes, user_password: str, owner_password: Optional[str]) -> io.BytesIO:
        pdf = pikepdf.open(io.BytesIO(content))
        
        output = io.BytesIO()
        pdf.save(
            output,
            encryption=pikepdf.Encryption(
                user=user_password,
                owner=owner_password or user_password,
                aes=True,
                R=6
            )
        )
        output.seek(0)
        pdf.close()
        
        return output

    @staticmethod
    def remove_password(content: bytes, password: str) -> io.BytesIO:
        pdf = pikepdf.open(io.BytesIO(content), password=password)
        
        output = io.BytesIO()
        pdf.save(output)
        output.seek(0)
        pdf.close()
        
        return output

    @staticmethod
    def get_info(content: bytes, filename: str) -> dict:
        pdf = pikepdf.open(io.BytesIO(content))
        metadata = pdf.docinfo
        
        result = {
            "filename": filename,
            "pages": len(pdf.pages),
            "encrypted": pdf.is_encrypted,
            "pdf_version": str(pdf.pdf_version),
            "metadata": {
                "title": str(metadata.get("/Title", "")) if metadata else None,
                "author": str(metadata.get("/Author", "")) if metadata else None,
                "subject": str(metadata.get("/Subject", "")) if metadata else None,
                "creator": str(metadata.get("/Creator", "")) if metadata else None,
                "producer": str(metadata.get("/Producer", "")) if metadata else None,
            }
        }
        
        pdf.close()
        return result

    @staticmethod
    def convert_to_image(
        content: bytes,
        format: Literal["png", "jpeg", "tiff"],
        dpi: int,
        pages: Optional[str]
    ) -> tuple[io.BytesIO, str, bool]:
        """
        Returns (buffer, extension, is_single_page)
        """
        pdf = fitz.open(stream=content, filetype="pdf")
        total_pages = len(pdf)
        
        if pages:
            page_numbers = parse_page_ranges(pages, total_pages)
        else:
            page_numbers = list(range(1, total_pages + 1))
        
        format_config = {
            "png": {"ext": "png", "mime": "image/png"},
            "jpeg": {"ext": "jpg", "mime": "image/jpeg"},
            "tiff": {"ext": "tiff", "mime": "image/tiff"}
        }
        
        config = format_config[format]
        zoom = dpi / 72
        matrix = fitz.Matrix(zoom, zoom)
        
        if len(page_numbers) == 1:
            page = pdf[page_numbers[0] - 1]
            pix = page.get_pixmap(matrix=matrix)
            
            if format == "jpeg":
                img_bytes = pix.tobytes("jpeg")
            elif format == "tiff":
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                tiff_buffer = io.BytesIO()
                img.save(tiff_buffer, format="TIFF")
                img_bytes = tiff_buffer.getvalue()
            else:
                img_bytes = pix.tobytes("png")
            
            pdf.close()
            return io.BytesIO(img_bytes), config['ext'], True, page_numbers[0], config['mime']
        
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for page_num in page_numbers:
                page = pdf[page_num - 1]
                pix = page.get_pixmap(matrix=matrix)
                
                if format == "jpeg":
                    img_bytes = pix.tobytes("jpeg")
                elif format == "tiff":
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    tiff_buffer = io.BytesIO()
                    img.save(tiff_buffer, format="TIFF")
                    img_bytes = tiff_buffer.getvalue()
                else:
                    img_bytes = pix.tobytes("png")
                
                zip_file.writestr(f"page_{page_num}.{config['ext']}", img_bytes)
        
        zip_buffer.seek(0)
        pdf.close()
        
        return zip_buffer, config['ext'], False, None, "application/zip"

    @staticmethod
    def convert_to_ofx(
        content: bytes,
        bank_id: str,
        account_id: str,
        account_type: str
    ) -> str:
        pdf = fitz.open(stream=content, filetype="pdf")
        
        full_text = ""
        for page in pdf:
            full_text += page.get_text()
        pdf.close()
        
        transactions = PdfService._extract_transactions_from_text(full_text)
        
        if not transactions:
            return None
        
        return PdfService._generate_ofx(transactions, bank_id, account_id, account_type)

    @staticmethod
    def extract_text(content: bytes) -> List[dict]:
        pdf = fitz.open(stream=content, filetype="pdf")
        
        pages_text = []
        for i, page in enumerate(pdf):
            pages_text.append({
                "page": i + 1,
                "text": page.get_text()
            })
        
        pdf.close()
        return pages_text

    @staticmethod
    def _extract_transactions_from_text(text: str) -> List[dict]:
        lines = text.split('\n')
        current_year = datetime.now().year
        
        skip_keywords = ['saldo do dia', 'saldo disponível', 'total', 'anterior', 'limite', 
                        'extrato', 'agência', 'conta', 'período', 'cliente', 'cpf', 'cnpj',
                        'solicitado em', 'ifood.com', 'atendimento', 'data movimentação']
        
        transactions = []
        for line in lines:
            line = line.strip()
            if not line or any(kw in line.lower() for kw in skip_keywords):
                continue
            transaction = PdfService._try_parse_transaction(line, current_year)
            if transaction:
                transactions.append(transaction)
        
        if not transactions:
            transactions = PdfService._parse_zoop_format(lines, current_year)
        
        return transactions

    @staticmethod
    def _parse_zoop_format(lines: List[str], current_year: int) -> List[dict]:
        transactions = []
        clean_lines = [l.strip() for l in lines if l.strip()]
        i = 0
        
        while i < len(clean_lines):
            line = clean_lines[i]
            date_match = re.match(r'^(\d{2}/\d{2}/\d{4})$', line)
            
            if date_match and i + 3 < len(clean_lines):
                date_str = date_match.group(1)
                tipo = clean_lines[i + 1]
                descricao = clean_lines[i + 2]
                valor_line = clean_lines[i + 3]
                
                valor_match = re.match(r'^(-?)R\$\s*([\d.]+,\d{2})$', valor_line)
                
                if valor_match:
                    try:
                        date = datetime.strptime(date_str, "%d/%m/%Y")
                        amount_str = valor_match.group(2).replace('.', '').replace(',', '.')
                        amount = float(amount_str)
                        if valor_match.group(1) == '-':
                            amount = -amount
                        
                        transactions.append({
                            "date": date,
                            "description": f"{tipo} - {descricao}",
                            "amount": amount
                        })
                        i += 4
                        continue
                    except (ValueError, IndexError):
                        pass
            i += 1
        
        return transactions

    @staticmethod
    def _try_parse_transaction(line: str, current_year: int) -> Optional[dict]:
        patterns = [
            (r'^(\d{2}/\d{2}/\d{4})\s+(.+?)\s+(-?)R\$\s*([\d.]+,\d{2})\s*$', '%d/%m/%Y', True),
            (r'^(\d{2}/\d{2}/\d{4})\s+(.+?)\s+R\$\s*(-?[\d.]+,\d{2})\s*$', '%d/%m/%Y', False),
            (r'^(\d{2}/\d{2}/\d{4})\s+(.+?)\s+(-?[\d.]+,\d{2})\s*$', '%d/%m/%Y', False),
            (r'^(\d{2}/\d{2}/\d{2})\s+(.+?)\s+(-?[\d.]+,\d{2})\s*$', '%d/%m/%y', False),
            (r'^(\d{2}/\d{2})\s+(.+?)\s+(-?[\d.]+,\d{2})\s*$', '%d/%m', False),
            (r'^(\d{4}-\d{2}-\d{2})\s+(.+?)\s+(-?[\d.]+,\d{2})\s*$', '%Y-%m-%d', False),
        ]
        
        for pattern, date_fmt, has_sign in patterns:
            match = re.match(pattern, line, re.IGNORECASE)
            if match:
                try:
                    date_str = match.group(1)
                    if date_fmt == '%d/%m':
                        date = datetime.strptime(f"{date_str}/{current_year}", "%d/%m/%Y")
                    else:
                        date = datetime.strptime(date_str, date_fmt)
                    
                    description = match.group(2).strip()
                    
                    if has_sign:
                        sign = match.group(3)
                        amount_str = match.group(4)
                        amount_str = f"{sign}{amount_str}"
                    else:
                        amount_str = match.group(3)
                    
                    amount_str = amount_str.replace('.', '').replace(',', '.')
                    amount = float(amount_str)
                    
                    if amount == 0:
                        continue
                    
                    return {"date": date, "description": description, "amount": amount}
                except ValueError:
                    continue
        
        return None

    @staticmethod
    def _generate_ofx(transactions: List[dict], bank_id: str, account_id: str, account_type: str) -> str:
        now = datetime.now()
        start_date = min(t["date"] for t in transactions) if transactions else now
        end_date = max(t["date"] for t in transactions) if transactions else now
        
        ofx = """OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<SIGNONMSGSRSV1>
<SONRS>
<STATUS>
<CODE>0
<SEVERITY>INFO
</STATUS>
<DTSERVER>{dtserver}
<LANGUAGE>POR
</SONRS>
</SIGNONMSGSRSV1>
<BANKMSGSRSV1>
<STMTTRNRS>
<TRNUID>{trnuid}
<STATUS>
<CODE>0
<SEVERITY>INFO
</STATUS>
<STMTRS>
<CURDEF>BRL
<BANKACCTFROM>
<BANKID>{bankid}
<ACCTID>{acctid}
<ACCTTYPE>{accttype}
</BANKACCTFROM>
<BANKTRANLIST>
<DTSTART>{dtstart}
<DTEND>{dtend}
""".format(
            dtserver=now.strftime("%Y%m%d%H%M%S"),
            trnuid=str(uuid.uuid4()).replace("-", "")[:32],
            bankid=bank_id,
            acctid=account_id,
            accttype=account_type,
            dtstart=start_date.strftime("%Y%m%d"),
            dtend=end_date.strftime("%Y%m%d")
        )
        
        for i, trans in enumerate(transactions):
            trntype = "CREDIT" if trans["amount"] >= 0 else "DEBIT"
            ofx += """<STMTTRN>
<TRNTYPE>{trntype}
<DTPOSTED>{dtposted}
<TRNAMT>{amount:.2f}
<FITID>{fitid}
<MEMO>{memo}
</STMTTRN>
""".format(
                trntype=trntype,
                dtposted=trans["date"].strftime("%Y%m%d"),
                amount=trans["amount"],
                fitid=f"{trans['date'].strftime('%Y%m%d')}{i:06d}",
                memo=trans["description"][:255]
            )
        
        balance = sum(t["amount"] for t in transactions)
        
        ofx += """</BANKTRANLIST>
<LEDGERBAL>
<BALAMT>{balance:.2f}
<DTASOF>{dtasof}
</LEDGERBAL>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>""".format(balance=balance, dtasof=end_date.strftime("%Y%m%d"))
        
        return ofx
