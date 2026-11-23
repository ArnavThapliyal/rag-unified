"""Local mock for the integration test: SearXNG JSON API + an HTTP proxy that serves synthetic documents
for http://<official-site>/... URLs. HTTPS CONNECT is refused, so nothing can leave the machine.
Every request is appended to requests.log (method, url, status) for evidence.

  python mock_server.py PORT LOGDIR
"""
import io
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT, LOGDIR = int(sys.argv[1]), sys.argv[2]
LOCK = threading.Lock()


def make_pdf(lines):
    """Minimal valid one-page PDF with Helvetica text (pdfminer-readable)."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 11 Tf 50 800 Td 14 TL " + " ".join(f"({esc(l)}) Tj T*" for l in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream"]
    out, offs = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, o in enumerate(objs, 1):
        offs.append(out.tell())
        out.write(f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1"))
    x = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for o in offs:
        out.write(f"{o:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode())
    return out.getvalue()


def make_xlsx():
    import openpyxl
    from datetime import datetime
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Sr. No.", "Name of the Company", "Symbol", "ISIN", "Date of Delisting", "Reason"])
    ws.append([1, "Jindal Cotex Limited", "JINDCOT", "INE904J01010", datetime(2017, 3, 24), "Compulsory delisting"])
    ws.append([2, "Penta Gold Limited", "PENTAGOLD", None, datetime(2007, 8, 15), "Compulsory"])
    ws.append([3, "Amalgamated Bean Coffee Trading Company Ltd", "ABCTC", None, datetime(2008, 2, 4), "Compulsory"])
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def make_scan():
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("L", (1700, 700), 255)
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 40)
    except OSError:
        f = ImageFont.load_default()
    for i, t in enumerate(["NATIONAL STOCK EXCHANGE OF INDIA LIMITED",
                           "Ref: NSE/LIST/2008/ 1234 March 10, 2008",
                           "The Company Secretary", "Scanmark Textiles Ltd.",
                           "Sub: Compulsory Delisting of equity shares",
                           "the Exchange has decided to delist the equity shares",
                           "with effect from March 28, 2008."]):
        d.text((40, 40 + i * 85), t, font=f, fill=0)
    b = io.BytesIO()
    img.save(b, format="PDF", resolution=150)
    return b.getvalue()


ORDER = make_pdf([
    'Before the Delisting Committee ("Committee")',
    "of National Stock Exchange of India Limited",
    "In the matter of the Company: Jindal Cotex Limited (JINDCOT)",
    "... failing which the Company shall be delisted from the Exchange.",
    "the Committee directed that the Equity shares of Jindal Cotex Limited (JINDCOT) be compulsorily",
    "delisted from the Exchange with effect from March 24, 2017 in terms of Regulation 22(1).",
    "Date: March 20, 2017"])
ORDER_AUG = make_pdf([
    'Before the Delisting Committee ("Committee")',
    "In the matter of the Company: Augusta Mills Limited (AUGMILL)",
    "the Equity shares of Augusta Mills Limited (AUGMILL) be compulsorily delisted from the Exchange",
    "with effect from August 21, 2008 in terms of Regulation 22(1).",
    "Date: August 14, 2008"])

LIST_HTML = b"""<html><head><title>Companies Delisted - NSE</title></head><body>
<h1>Delisted companies</h1>
<table><tr><th>Company Name</th><th>Symbol</th><th>ISIN</th><th>Date of Delisting</th><th>Reason</th></tr>
<tr><td>Jindal Cotex Limited</td><td>JINDCOT</td><td>INE904J01010</td><td>24-03-2017</td><td>Compulsory</td></tr>
<tr><td>Poddar Housing Ltd</td><td>PODDAR</td><td>INE888B01018</td><td>2008-03-04</td><td>Compulsory</td></tr>
<tr><td>Bhagyanagar Properties Ltd</td><td>BHAGYAPROP</td><td></td><td>15 August 2017</td><td>Voluntary exit offer</td></tr>
</table>
<a href="http://nsearchives.nseindia.com/content/delisting/order_JINDCOT.pdf">Order JINDCOT</a>
<a href="http://nsearchives.nseindia.com/content/delisting/order_AUGMILL.pdf">Order AUGMILL</a>
<a href="http://nsearchives.nseindia.com/content/delisting/delisted_list.xlsx">Delisted list</a>
<a href="http://nsearchives.nseindia.com/content/delisting/SOP_for_Delisting.pdf">SOP</a>
<a href="http://nsearchives.nseindia.com/content/delisting/defaulter_members.pdf">Defaulters</a>
</body></html>"""
NEWS = b"""<html><head><title>Jindal Cotex delisted from NSE</title></head><body>
<p>Jindal Cotex Limited INE904J01010 was compulsorily delisted on 24-03-2017.</p></body></html>"""
BROKERS = b"""<html><head><title>Defaulter / expelled members</title></head><body><table>
<tr><th>Name of Company</th><th>Symbol</th><th>Date of Delisting</th></tr>
<tr><td>Angel Broking Ltd</td><td>ANGEL</td><td>01-01-2017</td></tr></table></body></html>"""

DOCS = {  # url path -> (status, content-type, body, extra headers)
    "www.nseindia.com/static/delisting/companies-delisted": (200, "text/html", LIST_HTML, {}),
    "nsearchives.nseindia.com/content/delisting/order_JINDCOT.pdf": (200, "application/pdf", ORDER, {}),
    "nsearchives.nseindia.com/content/delisting/order_AUGMILL.pdf": (200, "application/pdf", ORDER_AUG, {}),
    "nsearchives.nseindia.com/content/delisting/delisted_list.xlsx":
        (200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", make_xlsx(), {}),
    "nsearchives.nseindia.com/content/delisting/SOP_for_Delisting.pdf": (200, "application/pdf", make_pdf(["SOP"]), {}),
    "nsearchives.nseindia.com/content/delisting/defaulter_members.pdf": (200, "application/pdf", make_pdf(["x"]), {}),
    "www.bseindia.com/delisting/scan_notice_2008.pdf": (200, "application/pdf", make_scan(), {}),
    "www.sebi.gov.in/orders/delisting-2008-gone.pdf": (404, "text/html", b"not found", {}),
    "www.sebi.gov.in/orders/delisting-throttled.pdf": (429, "text/html", b"slow down", {"Retry-After": "1"}),
    "www.moneycontrol.com/news/delisting-corrupt.pdf": (200, "application/pdf", b"%PDF-1.4 truncated", {}),
    "www.business-standard.com/article/jindal-cotex-delisting.html": (200, "text/html", NEWS, {}),
    "www.nseindia.com/static/delisting/defaulter-expelled-members": (200, "text/html", BROKERS, {}),
    "www.nseindia.com/static/delisting/slow-order.pdf": ("slow", "application/pdf", ORDER, {}),
    "www.sebi.gov.in/orders/delisting-retry-date.pdf":
        (503, "text/html", b"busy", {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
}
SCENARIO = {"name": "base"}


def results_for(engines):
    base = ["www.nseindia.com/static/delisting/companies-delisted",
            "www.bseindia.com/delisting/scan_notice_2008.pdf",
            "www.sebi.gov.in/orders/delisting-2008-gone.pdf",
            "www.sebi.gov.in/orders/delisting-throttled.pdf",
            "www.moneycontrol.com/news/delisting-corrupt.pdf",
            "www.business-standard.com/article/jindal-cotex-delisting.html",
            "www.nseindia.com/static/delisting/defaulter-expelled-members"]
    extra = {"slow": ["www.nseindia.com/static/delisting/slow-order.pdf"],
             "retrydate": ["www.sebi.gov.in/orders/delisting-retry-date.pdf"]}.get(SCENARIO["name"], [])
    urls = extra if extra else base
    return [{"url": "http://" + u, "title": f"Delisting document {i}", "content": "NSE delisting order",
             "engines": engines} for i, u in enumerate(urls)]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _log(self, status):
        with LOCK, open(f"{LOGDIR}/requests.log", "a") as f:
            f.write(json.dumps({"t": round(time.time(), 2), "method": self.command, "url": self.path,
                                "status": status}) + "\n")

    def _send(self, status, ctype, body, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        self._log(status)

    def do_CONNECT(self):  # HTTPS through the proxy: refused (no external access)
        self._send(403, "text/plain", b"external access disabled in QA sandbox")

    def do_GET(self):
        u = urlparse(self.path)
        host = u.hostname or "127.0.0.1"
        if host == "127.0.0.1" and u.path == "/scenario":
            SCENARIO["name"] = parse_qs(u.query).get("name", ["base"])[0]
            return self._send(200, "text/plain", SCENARIO["name"].encode())
        if host == "127.0.0.1" and u.path == "/search":
            q = parse_qs(u.query)
            engines = (q.get("engines", [""])[0]).split(",")
            page = int(q.get("pageno", ["1"])[0])
            res = results_for(engines) if page == 1 else []
            return self._send(200, "application/json", json.dumps(
                {"query": q.get("q", [""])[0], "results": res, "unresponsive_engines": []}).encode())
        key = f"{host}{u.path}"
        if key not in DOCS:
            return self._send(404, "text/html", b"no such mock document")
        status, ctype, body, headers = DOCS[key]
        if status == "slow":
            time.sleep(60)
            status = 200
        return self._send(status, ctype, body, headers)


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
