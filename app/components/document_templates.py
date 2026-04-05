from html import escape

TEMPLATES = {
    "Classic": {
        "font": "Georgia, 'Times New Roman', serif",
        "bg": "#ffffff",
        "text": "#1f2937",
        "muted": "#6b7280",
        "header_bg": "#f8fafc",
        "table_head_bg": "#f3f4f6",
        "border": "#e5e7eb",
    },
    "Ledger Dark": {
        "font": "'Trebuchet MS', 'Segoe UI', sans-serif",
        "bg": "#111827",
        "text": "#f9fafb",
        "muted": "#9ca3af",
        "header_bg": "#0b1220",
        "table_head_bg": "#1f2937",
        "border": "#374151",
    },
    "Merchant Modern": {
        "font": "'Gill Sans', 'Segoe UI', sans-serif",
        "bg": "#fefaf0",
        "text": "#1f2937",
        "muted": "#4b5563",
        "header_bg": "#fff7ed",
        "table_head_bg": "#ffedd5",
        "border": "#fed7aa",
    },
}


def money(value) -> str:
    if value is None:
        return "$0.00"
    return f"${float(value):,.2f}"


def _rows_html(items: list[dict], text_color: str, border_color: str) -> str:
    return "".join(
        [
            (
                f"<tr>"
                f"<td style='padding:10px;border-bottom:1px solid {border_color};color:{text_color};'>{escape(str(item['sku']))}</td>"
                f"<td style='padding:10px;border-bottom:1px solid {border_color};color:{text_color};'>{escape(str(item['title']))}</td>"
                f"<td style='padding:10px;border-bottom:1px solid {border_color};text-align:right;color:{text_color};'>{item['qty']}</td>"
                f"<td style='padding:10px;border-bottom:1px solid {border_color};text-align:right;color:{text_color};'>{money(item['unit_price'])}</td>"
                f"<td style='padding:10px;border-bottom:1px solid {border_color};text-align:right;color:{text_color};'>{money(item['line_total'])}</td>"
                f"</tr>"
            )
            for item in items
        ]
    )


def build_document_html(
    *,
    doc_type: str,
    template_name: str,
    accent_color: str,
    company_name: str,
    company_email: str,
    company_phone: str,
    company_website: str,
    logo_src: str = "",
    customer_label: str,
    document_number: str,
    document_date,
    source_label: str,
    source_number: str,
    source_marketplace: str,
    sold_at: str,
    notes: str,
    items: list[dict],
    subtotal: float,
    fees: float,
    shipping_cost: float,
    tax_amount: float = 0.0,
    tax_label: str = "Sales Tax",
    total: float,
) -> str:
    theme = TEMPLATES[template_name]
    doc_title = "INVOICE" if doc_type == "invoice" else "RECEIPT"
    notes_block = (
        f"<div style='margin-top:18px;white-space:pre-wrap;color:{theme['text']};'><strong>Notes</strong><br>{escape(notes)}</div>"
        if notes.strip()
        else ""
    )
    logo_block = (
        f"<div style='margin-bottom:12px;'><img src='{escape(logo_src)}' alt='Brand Logo' style='max-height:72px;max-width:280px;object-fit:contain;'></div>"
        if logo_src.strip()
        else ""
    )
    return f"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>{escape(doc_title)} {escape(document_number)}</title>
    <style>
      body {{
        font-family: {theme["font"]};
        background: {theme["bg"]};
        color: {theme["text"]};
        margin: 0;
        padding: 24px;
      }}
      .doc {{
        max-width: 980px;
        margin: 0 auto;
        border: 1px solid {theme["border"]};
        border-radius: 14px;
        overflow: hidden;
      }}
      .head {{
        padding: 20px;
        background: {theme["header_bg"]};
        border-bottom: 3px solid {accent_color};
      }}
      .grid {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 16px;
      }}
      .muted {{
        color: {theme["muted"]};
      }}
      table {{
        width: 100%;
        border-collapse: collapse;
      }}
      th {{
        text-align: left;
        padding: 10px;
        background: {theme["table_head_bg"]};
        color: {theme["text"]};
      }}
      .right {{
        text-align: right;
      }}
      .body {{
        padding: 20px;
      }}
      .totals {{
        margin-top: 18px;
        max-width: 320px;
        margin-left: auto;
      }}
      .totals td {{
        padding: 6px 0;
      }}
      .totals .total {{
        border-top: 2px solid {accent_color};
        font-weight: 700;
      }}
      .print-btn {{
        margin-top: 14px;
        padding: 10px 14px;
        border: 0;
        border-radius: 8px;
        background: {accent_color};
        color: white;
        font-weight: 700;
        cursor: pointer;
      }}
      @media print {{
        .no-print {{
          display: none;
        }}
        body {{
          padding: 0;
        }}
        .doc {{
          border: none;
          border-radius: 0;
        }}
      }}
    </style>
  </head>
  <body>
    <div class="doc">
      <div class="head">
        <div class="grid">
          <div>
            {logo_block}
            <div style="font-size:28px;font-weight:700;letter-spacing:1px;">{escape(company_name)}</div>
            <div class="muted">{escape(company_email)}</div>
            <div class="muted">{escape(company_phone)}</div>
            <div class="muted">{escape(company_website)}</div>
          </div>
          <div class="right">
            <div style="font-size:32px;font-weight:800;color:{accent_color};">{doc_title}</div>
            <div><strong>Document #:</strong> {escape(document_number)}</div>
            <div><strong>Date:</strong> {escape(str(document_date))}</div>
            <div><strong>Source:</strong> {escape(source_label)} {escape(source_number)}</div>
            <div><strong>Marketplace:</strong> {escape(source_marketplace)}</div>
            <div><strong>Sold At:</strong> {escape(sold_at)}</div>
          </div>
        </div>
      </div>
      <div class="body">
        <div style="margin-bottom:14px;"><strong>Bill To:</strong> {escape(customer_label)}</div>
        <table>
          <thead>
            <tr>
              <th>SKU</th>
              <th>Description</th>
              <th class="right">Qty</th>
              <th class="right">Unit Price</th>
              <th class="right">Line Total</th>
            </tr>
          </thead>
          <tbody>
            {_rows_html(items, theme["text"], theme["border"])}
          </tbody>
        </table>

        <table class="totals">
          <tr><td>Subtotal</td><td class="right">{money(subtotal)}</td></tr>
          <tr><td>Fees</td><td class="right">{money(fees)}</td></tr>
          <tr><td>Shipping</td><td class="right">{money(shipping_cost)}</td></tr>
          <tr><td>{escape(tax_label)}</td><td class="right">{money(tax_amount)}</td></tr>
          <tr class="total"><td>Total</td><td class="right">{money(total)}</td></tr>
        </table>
        {notes_block}
        <button class="print-btn no-print" onclick="window.print()">Print {doc_title}</button>
      </div>
    </div>
  </body>
</html>
"""
