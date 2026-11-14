"""
Financeiro da equipe: adiantamentos, adicionais, descontos, empréstimos
parcelados e a folha mensal (o mesmo cálculo da aba "Folha Mensal").

Folha de um funcionário no mês:
    Diarista:   valor das diárias/obras do mês + adicionais
    Mensalista: salário fixo + adicionais   (diárias lançadas não somam)
    a receber = bruto - adiantamentos - descontos - parcelas de empréstimos
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import get_usuario_atual
from app.models import (
    Apontamento, ApontamentoAdicional, ApontamentoItem, Eletricista,
    Emprestimo, LancamentoFinanceiro, get_db,
)

router = APIRouter(prefix="/financeiro", tags=["financeiro"])

TIPOS = {"adiantamento", "adicional", "desconto"}


def _intervalo(ano: int, mes: int):
    ini = date(ano, mes, 1)
    fim = date(ano + (mes == 12), mes % 12 + 1, 1)
    return ini, fim


def _idx(d: date) -> int:
    return d.year * 12 + d.month


def _parcela_no_mes(emp: Emprestimo, ano: int, mes: int) -> float:
    k = ano * 12 + mes - _idx(emp.inicio)
    if k < 0:
        return 0.0
    pago_antes = min(emp.valor_total, emp.valor_parcela * k)
    return round(max(0.0, min(emp.valor_parcela, emp.valor_total - pago_antes)), 2)


def _ser_emprestimo(emp: Emprestimo, nomes: dict) -> dict:
    hoje = date.today()
    k = _idx(hoje) - _idx(emp.inicio) + 1          # parcelas até o mês atual (inclusive)
    pago = round(min(emp.valor_total, emp.valor_parcela * max(0, k)), 2)
    restante = round(emp.valor_total - pago, 2)
    falta_parcelas = 0 if restante <= 0 else int(-(-restante // emp.valor_parcela))
    return {
        "id": emp.id,
        "eletricista_id": emp.eletricista_id,
        "eletricista_nome": nomes.get(emp.eletricista_id, ""),
        "descricao": emp.descricao,
        "valor_total": emp.valor_total,
        "valor_parcela": emp.valor_parcela,
        "inicio": emp.inicio.isoformat(),
        "observacao": emp.observacao,
        "pago_ate_hoje": pago,
        "restante": restante,
        "parcelas_restantes": falta_parcelas,
        "quitado": restante <= 0,
    }


def _ser_lanc(l: LancamentoFinanceiro, nomes: dict) -> dict:
    return {
        "id": l.id,
        "eletricista_id": l.eletricista_id,
        "eletricista_nome": nomes.get(l.eletricista_id, ""),
        "data": l.data.isoformat(),
        "tipo": l.tipo,
        "valor": l.valor,
        "descricao": l.descricao,
    }


def _nomes(db: Session) -> dict:
    return {e.id: e.nome for e in db.query(Eletricista.id, Eletricista.nome).all()}


# ── Lançamentos (adiantamento / adicional / desconto) ─────────────────────
class LancamentoSchema(BaseModel):
    eletricista_id: int
    data: date
    tipo: str
    valor: float
    descricao: Optional[str] = None


def _validar_lanc(data: LancamentoSchema, db: Session):
    if data.tipo not in TIPOS:
        raise HTTPException(400, "Tipo deve ser adiantamento, adicional ou desconto")
    if data.valor <= 0:
        raise HTTPException(400, "Informe um valor maior que zero")
    if not db.get(Eletricista, data.eletricista_id):
        raise HTTPException(404, "Funcionário não encontrado")


@router.get("/lancamentos")
def listar_lancamentos(
    ano: Optional[int] = None, mes: Optional[int] = Query(None, ge=1, le=12),
    eletricista_id: Optional[int] = None,
    db: Session = Depends(get_db), _=Depends(get_usuario_atual),
):
    q = db.query(LancamentoFinanceiro)
    if ano and mes:
        ini, fim = _intervalo(ano, mes)
        q = q.filter(LancamentoFinanceiro.data >= ini, LancamentoFinanceiro.data < fim)
    if eletricista_id:
        q = q.filter(LancamentoFinanceiro.eletricista_id == eletricista_id)
    nomes = _nomes(db)
    return [_ser_lanc(l, nomes) for l in q.order_by(LancamentoFinanceiro.data.desc(), LancamentoFinanceiro.id.desc()).all()]


@router.post("/lancamentos")
def criar_lancamento(data: LancamentoSchema, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    _validar_lanc(data, db)
    l = LancamentoFinanceiro(**data.model_dump())
    db.add(l)
    db.commit()
    db.refresh(l)
    return _ser_lanc(l, _nomes(db))


@router.put("/lancamentos/{id}")
def editar_lancamento(id: int, data: LancamentoSchema, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    l = db.get(LancamentoFinanceiro, id)
    if not l:
        raise HTTPException(404, "Lançamento não encontrado")
    _validar_lanc(data, db)
    for k, v in data.model_dump().items():
        setattr(l, k, v)
    db.commit()
    db.refresh(l)
    return _ser_lanc(l, _nomes(db))


@router.delete("/lancamentos/{id}")
def excluir_lancamento(id: int, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    l = db.get(LancamentoFinanceiro, id)
    if not l:
        raise HTTPException(404, "Lançamento não encontrado")
    db.delete(l)
    db.commit()
    return {"ok": True}


# ── Empréstimos parcelados ────────────────────────────────────────────────
class EmprestimoSchema(BaseModel):
    eletricista_id: int
    descricao: str
    valor_total: float
    valor_parcela: float
    inicio: date              # qualquer dia do mês da 1ª parcela
    observacao: Optional[str] = None


def _validar_emp(data: EmprestimoSchema, db: Session):
    if data.valor_total <= 0 or data.valor_parcela <= 0:
        raise HTTPException(400, "Valor total e parcela devem ser maiores que zero")
    if not db.get(Eletricista, data.eletricista_id):
        raise HTTPException(404, "Funcionário não encontrado")


@router.get("/emprestimos")
def listar_emprestimos(db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    nomes = _nomes(db)
    return [_ser_emprestimo(e, nomes) for e in db.query(Emprestimo).order_by(Emprestimo.inicio.desc()).all()]


@router.post("/emprestimos")
def criar_emprestimo(data: EmprestimoSchema, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    _validar_emp(data, db)
    d = data.model_dump()
    d["inicio"] = data.inicio.replace(day=1)
    e = Emprestimo(**d)
    db.add(e)
    db.commit()
    db.refresh(e)
    return _ser_emprestimo(e, _nomes(db))


@router.put("/emprestimos/{id}")
def editar_emprestimo(id: int, data: EmprestimoSchema, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    e = db.get(Emprestimo, id)
    if not e:
        raise HTTPException(404, "Empréstimo não encontrado")
    _validar_emp(data, db)
    for k, v in data.model_dump().items():
        setattr(e, k, v)
    e.inicio = data.inicio.replace(day=1)
    db.commit()
    db.refresh(e)
    return _ser_emprestimo(e, _nomes(db))


@router.delete("/emprestimos/{id}")
def excluir_emprestimo(id: int, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    e = db.get(Emprestimo, id)
    if not e:
        raise HTTPException(404, "Empréstimo não encontrado")
    db.delete(e)
    db.commit()
    return {"ok": True}


# ── Folha mensal ──────────────────────────────────────────────────────────
@router.get("/folha")
def folha(
    ano: int, mes: int = Query(..., ge=1, le=12),
    db: Session = Depends(get_db), _=Depends(get_usuario_atual),
):
    ini, fim = _intervalo(ano, mes)

    def por_elet(rows):
        return {k: float(v or 0) for k, v in rows}

    obras = por_elet(db.query(Apontamento.eletricista_id, func.sum(ApontamentoItem.valor))
                     .join(ApontamentoItem, ApontamentoItem.apontamento_id == Apontamento.id)
                     .filter(Apontamento.data >= ini, Apontamento.data < fim)
                     .group_by(Apontamento.eletricista_id).all())
    adic_apont = por_elet(db.query(Apontamento.eletricista_id, func.sum(ApontamentoAdicional.valor))
                          .join(ApontamentoAdicional, ApontamentoAdicional.apontamento_id == Apontamento.id)
                          .filter(Apontamento.data >= ini, Apontamento.data < fim)
                          .group_by(Apontamento.eletricista_id).all())
    dias = por_elet(db.query(Apontamento.eletricista_id, func.count(func.distinct(Apontamento.data)))
                    .filter(Apontamento.data >= ini, Apontamento.data < fim)
                    .group_by(Apontamento.eletricista_id).all())
    lancs = {}
    for eid, tipo, v in (db.query(LancamentoFinanceiro.eletricista_id, LancamentoFinanceiro.tipo, func.sum(LancamentoFinanceiro.valor))
                         .filter(LancamentoFinanceiro.data >= ini, LancamentoFinanceiro.data < fim)
                         .group_by(LancamentoFinanceiro.eletricista_id, LancamentoFinanceiro.tipo).all()):
        lancs[(eid, tipo)] = float(v or 0)
    parcelas = {}
    parcelas_desc = {}
    for emp in db.query(Emprestimo).all():
        p = _parcela_no_mes(emp, ano, mes)
        if p:
            parcelas[emp.eletricista_id] = parcelas.get(emp.eletricista_id, 0) + p
            parcelas_desc.setdefault(emp.eletricista_id, []).append(f"{emp.descricao}: R$ {p:.2f}")

    linhas = []
    for e in db.query(Eletricista).order_by(Eletricista.nome).all():
        mensalista = (e.tipo_contrato == "Mensalista")
        if mensalista and e.data_entrada and _idx(e.data_entrada) > ano * 12 + mes:
            mensalista_ativo_no_mes = False
        else:
            mensalista_ativo_no_mes = mensalista
        vl_obras = obras.get(e.id, 0.0)
        adicionais = adic_apont.get(e.id, 0.0) + lancs.get((e.id, "adicional"), 0.0)
        if mensalista:
            base = (e.salario_fixo or 0.0) if mensalista_ativo_no_mes else 0.0
        else:
            base = vl_obras
        bruto = base + adicionais
        adiant = lancs.get((e.id, "adiantamento"), 0.0)
        desc = lancs.get((e.id, "desconto"), 0.0)
        parc = parcelas.get(e.id, 0.0)
        movimento = any([vl_obras, adicionais, adiant, desc, parc]) or (mensalista_ativo_no_mes and e.status == "Ativo")
        if not movimento:
            continue
        linhas.append({
            "eletricista_id": e.id,
            "nome": e.nome,
            "tipo_contrato": e.tipo_contrato,
            "dias": int(dias.get(e.id, 0)),
            "valor_diarias": round(vl_obras, 2),
            "salario_fixo": round(base, 2) if mensalista else 0.0,
            "adicionais": round(adicionais, 2),
            "bruto": round(bruto, 2),
            "adiantamentos": round(adiant, 2),
            "descontos": round(desc, 2),
            "parcelas": round(parc, 2),
            "parcelas_detalhe": parcelas_desc.get(e.id, []),
            "a_receber": round(bruto - adiant - desc - parc, 2),
        })

    tot = lambda k: round(sum(l[k] for l in linhas), 2)
    return {
        "ano": ano, "mes": mes,
        "linhas": linhas,
        "totais": {k: tot(k) for k in ["valor_diarias", "salario_fixo", "adicionais", "bruto",
                                        "adiantamentos", "descontos", "parcelas", "a_receber"]},
    }


# ── PDF da folha (registro mensal, para enviar pelo WhatsApp) ─────────────
MESES_PT = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho", "Agosto",
            "Setembro", "Outubro", "Novembro", "Dezembro"]


def _br(v: float) -> str:
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def gerar_pdf_folha(f: dict, pagos: dict, gerado_por: str) -> bytes:
    import io
    from datetime import datetime, timedelta, timezone
    from xml.sax.saxutils import escape
    from reportlab.lib.colors import HexColor, white
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from app import marca

    AZUL, AMARELO, CINZA, VERDE, VERM = (HexColor(marca.AZUL), HexColor(marca.AMARELO), HexColor("#6b7785"),
                                         HexColor("#15803d"), HexColor("#b91c1c"))
    W, H = landscape(A4)
    M, HEAD = 30, 78
    ano, mes, t, linhas = f["ano"], f["mes"], f["totais"], f["linhas"]
    titulo_mes = f"{MESES_PT[mes - 1]} de {ano}"
    agora = datetime.now(timezone(timedelta(hours=-3))).strftime("%d/%m/%Y às %H:%M")

    def cabecalho(c, doc):
        c.saveState()
        c.setFillColor(AZUL)
        c.rect(0, H - HEAD, W, HEAD, stroke=0, fill=1)
        c.setFillColor(AMARELO)
        c.rect(0, H - HEAD - 4, W, 4, stroke=0, fill=1)
        lh = 46
        try:
            c.drawImage(marca.LOGO_MARCA, M - 4, H - 62, width=lh * marca.LOGO_MARCA_PROP, height=lh, mask="auto")
        except Exception:
            pass
        x = M + lh * marca.LOGO_MARCA_PROP + 10
        c.setFillColor(AMARELO)
        c.setFont("Helvetica-BoldOblique", 11)
        c.drawString(x, H - 32, "ELÉTRICA")
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 15)
        c.drawString(x, H - 50, "Folha de pagamento")
        c.setFont("Helvetica-Oblique", 8)
        c.drawString(x, H - 63, marca.SLOGAN)
        c.setFont("Helvetica-Bold", 18)
        c.drawRightString(W - M, H - 38, titulo_mes)
        c.setFont("Helvetica", 8.5)
        c.setFillColor(HexColor("#c7d2fe"))
        c.drawRightString(W - M, H - 54, f"{marca.EMPRESA} · CNPJ {marca.CNPJ}")
        c.drawRightString(W - M, H - 66, f"{len(linhas)} funcionário(s)")
        c.setFillColor(CINZA)
        c.setFont("Helvetica", 7.5)
        c.drawString(M, 18, f"Gerado em {agora}{' por ' + gerado_por if gerado_por else ''} · registro mensal da folha")
        c.drawRightString(W - M, 18, f"Página {doc.page}")
        c.restoreState()

    st = lambda **k: ParagraphStyle("x", **{"fontName": "Helvetica", "fontSize": 8.5, "leading": 10.5, **k})
    story = []

    # quadros de resumo (como o dashboard da folha)
    caixas = [("TOTAL BRUTO", t.get("bruto", 0), "diárias + fixos + adicionais", AZUL, False),
              ("ADIANTAMENTOS", t.get("adiantamentos", 0), "já pagos no mês", AZUL, False),
              ("DESCONTOS + PARCELAS", (t.get("descontos") or 0) + (t.get("parcelas") or 0), "empréstimos e avulsos", AZUL, False),
              ("A PAGAR", t.get("a_receber", 0), "valor final a receber", AMARELO, True)]
    cel = []
    for rot, v, sub, cor, destaque in caixas:
        cel.append([Paragraph(rot, st(fontName="Helvetica-Bold", fontSize=7.5, textColor=AMARELO if destaque else CINZA)),
                    Paragraph(f"R$ {_br(v)}", st(fontName="Helvetica-Bold", fontSize=16, leading=20,
                                                   textColor=white if destaque else AZUL)),
                    Paragraph(sub, st(fontSize=7.5, textColor=HexColor("#c7d2fe") if destaque else CINZA))])
    larg = (W - 2 * M - 3 * 10) / 4
    quadros = Table([[Table([[x] for x in c], colWidths=[larg - 20]) for c in cel]],
                    colWidths=[larg + 10] * 3 + [larg])
    qs = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 10)]
    quadros.setStyle(TableStyle(qs))
    for k, (_, _, _, _, destaque) in enumerate(caixas):
        quadros._cellvalues[0][k].setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), AZUL if destaque else HexColor("#f1f5ff")),
            ("LEFTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (0, 0), 9),
            ("BOTTOMPADDING", (0, -1), (-1, -1), 9), ("TOPPADDING", (0, 1), (-1, -1), 1)]))
    story += [quadros, Spacer(0, 14)]

    th = st(fontName="Helvetica-Bold", fontSize=7.5, textColor=AMARELO, alignment=2)
    th_l = st(fontName="Helvetica-Bold", fontSize=7.5, textColor=AMARELO)
    cab = [Paragraph("FUNCIONÁRIO", th_l), Paragraph("DIÁRIAS / FIXO", th), Paragraph("ADICIONAIS", th),
           Paragraph("BRUTO", th), Paragraph("ADIANT.", th), Paragraph("DESCONTOS", th), Paragraph("PARCELAS", th),
           Paragraph("A RECEBER", th), Paragraph("PAGO", st(fontName="Helvetica-Bold", fontSize=7.5, textColor=AMARELO, alignment=1))]
    dados = [cab]
    neg = lambda v: f"- {_br(v)}" if v else "—"
    for l in linhas:
        mens = l["tipo_contrato"] == "Mensalista"
        nome = (f"<b>{escape(l['nome'])}</b><br/><font size=7 color='#6b7785'>"
                + ("Mensalista" if mens else f"{l['dias']} dia(s) apontado(s)") + "</font>")
        if l.get("parcelas_detalhe"):
            nome += f"<br/><font size=6.5 color='#6b7785'>{escape(' | '.join(l['parcelas_detalhe']))}</font>"
        pago = pagos.get(str(l["eletricista_id"]))
        dados.append([Paragraph(nome, st()),
                      (_br(l["salario_fixo"]) + " (fixo)") if mens else _br(l["valor_diarias"]),
                      ("+ " + _br(l["adicionais"])) if l["adicionais"] else "—",
                      _br(l["bruto"]), neg(l["adiantamentos"]), neg(l["descontos"]), neg(l["parcelas"]),
                      "R$ " + _br(l["a_receber"]),
                      "PAGO" if pago else ""])
    dados.append([Paragraph("<b>TOTAL DA FOLHA</b>", st(textColor=white)),
                  _br((t.get("valor_diarias") or 0) + (t.get("salario_fixo") or 0)), _br(t.get("adicionais") or 0),
                  _br(t.get("bruto") or 0), neg(t.get("adiantamentos") or 0), neg(t.get("descontos") or 0),
                  neg(t.get("parcelas") or 0), "R$ " + _br(t.get("a_receber") or 0),
                  f"{sum(1 for l in linhas if pagos.get(str(l['eletricista_id'])))}/{len(linhas)}"])
    resto = W - 2 * M - 150
    cw = [150] + [resto * p for p in (0.14, 0.12, 0.13, 0.12, 0.12, 0.12, 0.16)] + [resto * 0.09]
    tb = Table(dados, colWidths=cw, repeatRows=1)
    n = len(dados)
    sty = [("BACKGROUND", (0, 0), (-1, 0), AZUL), ("FONT", (1, 1), (-1, -1), "Helvetica", 8.5),
           ("ALIGN", (1, 1), (-2, -1), "RIGHT"), ("ALIGN", (-1, 0), (-1, -1), "CENTER"),
           ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LINEBELOW", (0, 1), (-1, -2), 0.4, HexColor("#e2e8f0")),
           ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
           ("FONT", (7, 1), (7, -2), "Helvetica-Bold", 9), ("TEXTCOLOR", (7, 1), (7, -2), AZUL),
           ("TEXTCOLOR", (4, 1), (6, -2), VERM), ("FONT", (-1, 1), (-1, -2), "Helvetica-Bold", 7.5),
           ("TEXTCOLOR", (-1, 1), (-1, -2), VERDE),
           ("BACKGROUND", (0, n - 1), (-1, n - 1), AZUL), ("TEXTCOLOR", (1, n - 1), (-1, n - 1), white),
           ("FONT", (1, n - 1), (-1, n - 1), "Helvetica-Bold", 9), ("TEXTCOLOR", (7, n - 1), (7, n - 1), AMARELO),
           ("FONT", (7, n - 1), (7, n - 1), "Helvetica-Bold", 11)]
    for r in range(2, n - 1, 2):
        sty.append(("BACKGROUND", (0, r), (-1, r), HexColor("#f6f8fd")))
    for r, l in enumerate(linhas, start=1):
        if l["a_receber"] < 0:
            sty.append(("TEXTCOLOR", (7, r), (7, r), VERM))
    tb.setStyle(TableStyle(sty))
    story.append(tb)

    nota = Paragraph("Mensalistas recebem o fixo + adicionais (diárias lançadas para eles entram só no custo das obras). "
                     "PAGO = comprovante de pagamento anexado no app.", st(fontSize=7.5, textColor=CINZA))
    lw = (W - 2 * M - 60) / 2
    ass = Table([["", "", ""], ["Responsável pela folha", "", "Aprovação · Diretoria"]], colWidths=[lw, 60, lw],
                rowHeights=[30, 14])
    ass.setStyle(TableStyle([("LINEABOVE", (0, 1), (0, 1), 0.6, CINZA), ("LINEABOVE", (2, 1), (2, 1), 0.6, CINZA),
                             ("FONT", (0, 1), (-1, 1), "Helvetica", 8), ("TEXTCOLOR", (0, 1), (-1, 1), CINZA),
                             ("ALIGN", (0, 1), (-1, 1), "CENTER")]))
    story.append(KeepTogether([Spacer(0, 8), nota, Spacer(0, 16), ass]))

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=(W, H), leftMargin=M, rightMargin=M, topMargin=HEAD + 18, bottomMargin=34,
                      title=f"Folha {titulo_mes} - AZ Elétrica", author="AZ Elétrica").build(
        story, onFirstPage=cabecalho, onLaterPages=cabecalho)
    return buf.getvalue()


@router.get("/folha/pdf")
def folha_pdf(ano: int, mes: int = Query(..., ge=1, le=12), db: Session = Depends(get_db),
              usuario=Depends(get_usuario_atual)):
    from fastapi.responses import Response
    from app.models import Comprovante
    f = folha(ano=ano, mes=mes, db=db, _=None)
    pagos = {str(e): True for (e,) in db.query(Comprovante.eletricista_id)
             .filter(Comprovante.ano == ano, Comprovante.mes == mes).distinct().all()}
    pdf = gerar_pdf_folha(f, pagos, getattr(usuario, "nome", "") or "")
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="Folha_AZ_{ano}-{mes:02d}.pdf"'})
