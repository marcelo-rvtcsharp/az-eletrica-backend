from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from pydantic import BaseModel
from typing import List, Optional
from datetime import date, datetime
import uuid
from app.models import Orcamento, OrcamentoItem, get_db
from app.auth import get_usuario_atual

router = APIRouter(prefix="/orcamentos", tags=["orcamentos"])


class ItemOrcSchema(BaseModel):
    descricao: str
    und: str = "UND"
    qtd: float = 0
    vunit: float = 0
    ordem: int = 0


class OrcamentoSchema(BaseModel):
    cliente_nome: Optional[str] = None
    cliente_cnpj: Optional[str] = None
    cliente_endereco: Optional[str] = None
    obra_descricao: Optional[str] = None
    data: Optional[date] = None
    validade: int = 10
    status: str = "Rascunho"
    obs: Optional[str] = None
    itens: List[ItemOrcSchema] = []


def serializar(o: Orcamento) -> dict:
    total = sum(i.qtd * i.vunit for i in o.itens)
    return {
        "id": o.id,
        "numero": o.numero,
        "cliente_nome": o.cliente_nome,
        "cliente_cnpj": o.cliente_cnpj,
        "cliente_endereco": o.cliente_endereco,
        "obra_descricao": o.obra_descricao,
        "data": str(o.data) if o.data else None,
        "validade": o.validade,
        "status": o.status,
        "obs": o.obs,
        "total": round(total, 2),
        "criado_em": o.criado_em.isoformat() if o.criado_em else None,
        "atualizadoEm": o.atualizadoEm.isoformat() if o.atualizadoEm else None,
        "itens": [
            {
                "id": i.id,
                "descricao": i.descricao,
                "und": i.und,
                "qtd": i.qtd,
                "vunit": i.vunit,
                "vltotal": round(i.qtd * i.vunit, 2),
                "ordem": i.ordem,
            }
            for i in sorted(o.itens, key=lambda x: x.ordem)
        ],
    }


@router.get("/")
def listar(db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    orcs = db.query(Orcamento).options(joinedload(Orcamento.itens)).order_by(Orcamento.criado_em.desc()).all()
    return [serializar(o) for o in orcs]


@router.post("/")
def criar(data: OrcamentoSchema, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    numero = "ORC-" + uuid.uuid4().hex[:8].upper()
    orc = Orcamento(
        numero=numero,
        cliente_nome=data.cliente_nome,
        cliente_cnpj=data.cliente_cnpj,
        cliente_endereco=data.cliente_endereco,
        obra_descricao=data.obra_descricao,
        data=data.data or datetime.utcnow().date(),
        validade=data.validade,
        status=data.status,
        obs=data.obs,
    )
    db.add(orc)
    db.flush()
    for idx, item in enumerate(data.itens):
        db.add(OrcamentoItem(orcamento_id=orc.id, ordem=idx, **item.model_dump(exclude={"ordem"})))
    db.commit()
    db.refresh(orc)
    orc = db.query(Orcamento).options(joinedload(Orcamento.itens)).filter(Orcamento.id == orc.id).first()
    return serializar(orc)


@router.get("/{id}")
def buscar(id: int, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    orc = db.query(Orcamento).options(joinedload(Orcamento.itens)).filter(Orcamento.id == id).first()
    if not orc:
        raise HTTPException(404, "Orçamento não encontrado")
    return serializar(orc)


@router.put("/{id}")
def atualizar(id: int, data: OrcamentoSchema, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    orc = db.query(Orcamento).filter(Orcamento.id == id).first()
    if not orc:
        raise HTTPException(404, "Orçamento não encontrado")
    orc.cliente_nome = data.cliente_nome
    orc.cliente_cnpj = data.cliente_cnpj
    orc.cliente_endereco = data.cliente_endereco
    orc.obra_descricao = data.obra_descricao
    orc.data = data.data
    orc.validade = data.validade
    orc.status = data.status
    orc.obs = data.obs
    orc.atualizadoEm = datetime.utcnow()
    # Recria itens
    db.query(OrcamentoItem).filter(OrcamentoItem.orcamento_id == id).delete()
    for idx, item in enumerate(data.itens):
        db.add(OrcamentoItem(orcamento_id=orc.id, ordem=idx, **item.model_dump(exclude={"ordem"})))
    db.commit()
    orc = db.query(Orcamento).options(joinedload(Orcamento.itens)).filter(Orcamento.id == id).first()
    return serializar(orc)


@router.delete("/{id}")
def deletar(id: int, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    orc = db.query(Orcamento).filter(Orcamento.id == id).first()
    if not orc:
        raise HTTPException(404, "Orçamento não encontrado")
    db.delete(orc)
    db.commit()
    return {"ok": True}


# ── PDF do orçamento (gerado no servidor para poder ser compartilhado) ─────
def _fmt(v: float) -> str:
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def gerar_pdf_orcamento(o: Orcamento) -> bytes:
    import io
    from xml.sax.saxutils import escape
    from reportlab.lib.colors import HexColor, white
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, KeepTogether
    from app import marca

    AZUL, AMARELO, CINZA = HexColor(marca.AZUL), HexColor(marca.AMARELO), HexColor("#6b7785")
    W, H = A4
    M = 34
    HEAD = 96
    itens = sorted(o.itens, key=lambda x: x.ordem)
    total = sum((i.qtd or 0) * (i.vunit or 0) for i in itens)
    data_txt = o.data.strftime("%d/%m/%Y") if o.data else ""

    def cabecalho(c, doc):
        c.saveState()
        c.setFillColor(AZUL)
        c.rect(0, H - HEAD, W, HEAD, stroke=0, fill=1)
        c.setFillColor(AMARELO)
        c.rect(0, H - HEAD - 4, W, 4, stroke=0, fill=1)
        lh = 74
        try:
            c.drawImage(marca.LOGO_COMPLETO, M - 6, H - HEAD + 11, width=lh * 600 / 393, height=lh, mask="auto")
        except Exception:
            c.setFillColor(AMARELO)
            c.setFont("Helvetica-Bold", 24)
            c.drawString(M, H - 50, "AZ ELÉTRICA")
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 10)
        c.drawRightString(W - M, H - 26, "ORÇAMENTO")
        c.setFillColor(AMARELO)
        c.setFont("Helvetica-Bold", 15)
        c.drawRightString(W - M, H - 44, o.numero or str(o.id))
        c.setFillColor(white)
        c.setFont("Helvetica", 8.5)
        c.drawRightString(W - M, H - 58, f"Data: {data_txt} · Validade: {o.validade or 10} dias")
        c.setFont("Helvetica", 7.5)
        c.setFillColor(HexColor("#c7d2fe"))
        c.drawRightString(W - M, H - 72, f"{marca.EMPRESA} · CNPJ {marca.CNPJ}")
        c.drawRightString(W - M, H - 83, marca.CONTATO)
        # rodapé
        c.setFillColor(CINZA)
        c.setFont("Helvetica", 7.5)
        c.drawString(M, 20, f"{marca.EMPRESA} · {marca.SLOGAN}")
        c.drawRightString(W - M, 20, f"Página {doc.page}")
        c.restoreState()

    st = lambda **k: ParagraphStyle("x", **{"fontName": "Helvetica", "fontSize": 8.5, "leading": 11, **k})
    s_cli = st(fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=HexColor("#1a1a2e"))
    s_txt = st(textColor=HexColor("#475569"))
    s_obj = st(fontName="Helvetica-Bold", fontSize=9.5, leading=12, textColor=AZUL)
    s_cel = st(fontSize=8.5, leading=10.5)
    s_th = st(fontName="Helvetica-Bold", fontSize=7.5, textColor=AMARELO)

    story = []
    cli = [Paragraph(escape(o.cliente_nome or "—") + (f" <font size=8.5 color='#475569'>· CNPJ/CPF: {escape(o.cliente_cnpj)}</font>" if o.cliente_cnpj else ""), s_cli)]
    if o.cliente_endereco:
        cli.append(Paragraph("Endereço: " + escape(o.cliente_endereco), s_txt))
    if o.obra_descricao:
        cli.append(Spacer(0, 3))
        cli.append(Paragraph("Objeto: " + escape(o.obra_descricao), s_obj))
    caixa = Table([[cli]], colWidths=[W - 2 * M])
    caixa.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), HexColor("#f1f5ff")),
                               ("LINEBEFORE", (0, 0), (0, -1), 3, AZUL),
                               ("LEFTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 8),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story += [caixa, Spacer(0, 12)]

    cw = [26, None, 40, 44, 70, 78]
    cw[1] = W - 2 * M - sum(x for x in cw if x)
    dados = [[Paragraph(t, s_th) for t in ("Nº", "DESCRIÇÃO", "UND", "QTD", "VL UNIT (R$)", "VL TOTAL (R$)")]]
    for n, it in enumerate(itens, 1):
        vt = (it.qtd or 0) * (it.vunit or 0)
        dados.append([f"{n:02d}", Paragraph(escape(it.descricao or ""), s_cel), it.und or "",
                      _fmt(it.qtd or 0) if (it.qtd or 0) % 1 else f"{int(it.qtd or 0)}",
                      _fmt(it.vunit or 0), _fmt(vt)])
    if len(dados) == 1:
        dados.append(["", Paragraph("Nenhum item", s_txt), "", "", "", ""])
    t = Table(dados, colWidths=cw, repeatRows=1)
    sty = [("BACKGROUND", (0, 0), (-1, 0), AZUL),
           ("FONT", (0, 1), (-1, -1), "Helvetica", 8.5),
           ("ALIGN", (0, 0), (0, -1), "CENTER"), ("ALIGN", (2, 0), (2, -1), "CENTER"),
           ("ALIGN", (3, 1), (-1, -1), "RIGHT"), ("FONT", (5, 1), (5, -1), "Helvetica-Bold", 8.5),
           ("TEXTCOLOR", (5, 1), (5, -1), AZUL), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
           ("LINEBELOW", (0, 1), (-1, -1), 0.4, HexColor("#e2e8f0")),
           ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]
    for r in range(2, len(dados), 2):
        sty.append(("BACKGROUND", (0, r), (-1, r), HexColor("#f6f8fd")))
    t.setStyle(TableStyle(sty))
    story.append(t)

    tot = Table([["VALOR TOTAL", f"R$ {_fmt(total)}"]], colWidths=[W - 2 * M - 160, 160])
    tot.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), AZUL),
                             ("FONT", (0, 0), (0, 0), "Helvetica-Bold", 10), ("TEXTCOLOR", (0, 0), (0, 0), white),
                             ("FONT", (1, 0), (1, 0), "Helvetica-Bold", 15), ("TEXTCOLOR", (1, 0), (1, 0), AMARELO),
                             ("ALIGN", (1, 0), (1, 0), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                             ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                             ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    obs = f"<b>OBS:</b> Orçamento válido por {o.validade or 10} dias. — Amontada – CE, {data_txt}"
    if o.obs:
        obs += "<br/>" + escape(o.obs).replace("\n", "<br/>")
    caixa_obs = Table([[Paragraph(obs, st(textColor=HexColor("#504000")))]], colWidths=[W - 2 * M])
    caixa_obs.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), HexColor("#FFF6CC")),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 7),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    lw = (W - 2 * M - 40) / 2
    ass = Table([["", "", ""], ["Assinatura · AZ Elétrica", "", "Aceite do cliente"]], colWidths=[lw, 40, lw],
                rowHeights=[34, 14])
    ass.setStyle(TableStyle([("LINEABOVE", (0, 1), (0, 1), 0.6, CINZA), ("LINEABOVE", (2, 1), (2, 1), 0.6, CINZA),
                             ("FONT", (0, 1), (-1, 1), "Helvetica", 8), ("TEXTCOLOR", (0, 1), (-1, 1), CINZA),
                             ("ALIGN", (0, 1), (-1, 1), "CENTER"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(KeepTogether([tot, Spacer(0, 10), caixa_obs, Spacer(0, 18), ass]))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=M, rightMargin=M, topMargin=HEAD + 18, bottomMargin=40,
                            title=f"Orçamento {o.numero or o.id} - {o.cliente_nome or ''}", author="AZ Elétrica")
    doc.build(story, onFirstPage=cabecalho, onLaterPages=cabecalho)
    return buf.getvalue()


@router.get("/{id}/pdf")
def pdf(id: int, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    from fastapi.responses import Response
    orc = db.query(Orcamento).options(joinedload(Orcamento.itens)).filter(Orcamento.id == id).first()
    if not orc:
        raise HTTPException(404, "Orçamento não encontrado")
    nome = f"Orcamento_{orc.numero or orc.id}.pdf"
    return Response(gerar_pdf_orcamento(orc), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{nome}"'})
