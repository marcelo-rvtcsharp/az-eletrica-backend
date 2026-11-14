"""
Pagamento mensal de cada funcionário:
- comprovantes (foto ou PDF) guardados no banco, por funcionário e mês;
- PDF de resumo do mês: diárias, horas extras, calendário com dia e obra, valor a receber.
"""
import calendar
import io
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.auth import get_usuario_atual
from app.models import Apontamento, ApontamentoItem, Comprovante, Eletricista, get_db

router = APIRouter(tags=["pagamentos"])

MAX_BYTES = 8 * 1024 * 1024
TIPOS_OK = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "application/pdf"}
MESES = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho", "Agosto",
         "Setembro", "Outubro", "Novembro", "Dezembro"]
FORTALEZA = timezone(timedelta(hours=-3))


def _ser(c: Comprovante) -> dict:
    return {"id": c.id, "eletricista_id": c.eletricista_id, "ano": c.ano, "mes": c.mes,
            "nome_arquivo": c.nome_arquivo, "mime": c.mime, "tamanho": c.tamanho, "valor": c.valor,
            "data_pagamento": c.data_pagamento.isoformat() if c.data_pagamento else None,
            "observacao": c.observacao, "criado_em": c.criado_em.isoformat() if c.criado_em else None}


# ── Comprovantes ──────────────────────────────────────────────────────────
@router.get("/comprovantes")
def listar(eletricista_id: int, ano: int, mes: int = Query(..., ge=1, le=12),
           db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    cs = (db.query(Comprovante).filter(Comprovante.eletricista_id == eletricista_id,
                                       Comprovante.ano == ano, Comprovante.mes == mes)
          .order_by(Comprovante.criado_em).all())
    return [_ser(c) for c in cs]


@router.get("/comprovantes/status")
def status(ano: int, mes: int = Query(..., ge=1, le=12), db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    """Quantos comprovantes cada funcionário tem no mês (para o selo 'pago' na folha)."""
    linhas = (db.query(Comprovante.eletricista_id, func.count(Comprovante.id), func.sum(Comprovante.valor))
              .filter(Comprovante.ano == ano, Comprovante.mes == mes)
              .group_by(Comprovante.eletricista_id).all())
    return {str(e): {"qtd": int(n), "valor": round(float(v or 0), 2)} for e, n, v in linhas}


@router.post("/comprovantes")
async def enviar(
    eletricista_id: int = Form(...), ano: int = Form(...), mes: int = Form(...),
    valor: Optional[float] = Form(None), data_pagamento: Optional[date] = Form(None),
    observacao: Optional[str] = Form(None), arquivo: UploadFile = File(...),
    db: Session = Depends(get_db), _=Depends(get_usuario_atual),
):
    if not 1 <= mes <= 12:
        raise HTTPException(400, "Mês inválido")
    if not db.get(Eletricista, eletricista_id):
        raise HTTPException(404, "Funcionário não encontrado")
    dados = await arquivo.read()
    if not dados:
        raise HTTPException(400, "Arquivo vazio")
    if len(dados) > MAX_BYTES:
        raise HTTPException(400, "Arquivo maior que 8 MB")
    mime = (arquivo.content_type or "").lower()
    if mime not in TIPOS_OK:
        raise HTTPException(400, "Envie uma foto (JPG/PNG) ou um PDF")
    c = Comprovante(eletricista_id=eletricista_id, ano=ano, mes=mes, nome_arquivo=arquivo.filename or "comprovante",
                    mime=mime, tamanho=len(dados), dados=dados, valor=valor, data_pagamento=data_pagamento,
                    observacao=(observacao or "").strip() or None)
    db.add(c)
    db.commit()
    db.refresh(c)
    return _ser(c)


@router.get("/comprovantes/{id}/arquivo")
def baixar(id: int, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    c = db.get(Comprovante, id)
    if not c:
        raise HTTPException(404, "Comprovante não encontrado")
    return Response(content=c.dados, media_type=c.mime,
                    headers={"Content-Disposition": f'inline; filename="{_ascii(c.nome_arquivo)}"'})


@router.delete("/comprovantes/{id}")
def excluir(id: int, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    c = db.get(Comprovante, id)
    if not c:
        raise HTTPException(404, "Comprovante não encontrado")
    db.delete(c)
    db.commit()
    return {"ok": True}


def _ascii(s: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode() or "arquivo"


# ── Resumo do mês (dados) ─────────────────────────────────────────────────
_RE_HE_OBS = re.compile(r"(\d+(?:[.,]\d+)?)\s*h\s*extra\s*\(R\$\s*([\d.,]+)\)", re.I)
_RE_HORAS = re.compile(r"(\d+(?:[.,]\d+)?)\s*h", re.I)


def _num(s: str) -> float:
    s = s.strip()
    if "," in s and "." in s:          # 1.234,56
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def resumo_mes(db: Session, eid: int, ano: int, mes: int) -> dict:
    e = db.get(Eletricista, eid)
    if not e:
        raise HTTPException(404, "Funcionário não encontrado")
    ini = date(ano, mes, 1)
    fim = date(ano + (mes == 12), mes % 12 + 1, 1)
    aptos = (db.query(Apontamento)
             .options(joinedload(Apontamento.itens).joinedload(ApontamentoItem.obra), joinedload(Apontamento.adicionais))
             .filter(Apontamento.eletricista_id == eid, Apontamento.data >= ini, Apontamento.data < fim)
             .order_by(Apontamento.data).all())
    dias = []
    for a in aptos:
        he_h = he_v = 0.0
        for m in _RE_HE_OBS.finditer(a.observacao or ""):       # horas extras vindas da planilha
            he_h += _num(m.group(1))
            he_v += _num(m.group(2))
        he_v_planilha = he_v
        adicionais = 0.0
        for ad in a.adicionais:                                   # adicionais lançados no app
            if ad.tipo == "hora_extra":
                he_v += ad.valor
                mh = _RE_HORAS.search(ad.descricao or "")
                if mh:
                    he_h += _num(mh.group(1))
            else:
                adicionais += ad.valor
        itens = [{"obra": (i.obra.nome if i.obra else "?"), "valor": i.valor} for i in a.itens]
        total_itens = sum(i["valor"] for i in itens)
        diarias = round((total_itens - he_v_planilha) / e.valor_diaria, 2) if e.valor_diaria else None
        dias.append({"data": a.data, "itens": itens, "diarias": diarias, "he_horas": round(he_h, 2),
                     "he_valor": round(he_v, 2), "adicionais": round(adicionais, 2),
                     "total": round(total_itens + sum(x.valor for x in a.adicionais), 2),
                     "observacao": a.observacao})

    from app.routes.financeiro import folha as _folha
    f = _folha(ano=ano, mes=mes, db=db, _=None)
    linha = next((l for l in f["linhas"] if l["eletricista_id"] == eid), None)
    comps = (db.query(Comprovante).filter(Comprovante.eletricista_id == eid, Comprovante.ano == ano,
                                          Comprovante.mes == mes).order_by(Comprovante.criado_em).all())
    return {"func": e, "ano": ano, "mes": mes, "dias": dias, "folha": linha, "comprovantes": comps}


# ── PDF ───────────────────────────────────────────────────────────────────
def _brl(v: float) -> str:
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def _qtd(v: float) -> str:
    if v is None:
        return "—"
    if abs(v - round(v)) < 0.01:
        return str(int(round(v)))
    if abs(v - 0.5) < 0.01:
        return "½"
    return f"{v:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def gerar_pdf(r: dict) -> bytes:
    from reportlab.lib.colors import HexColor, white
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    AZUL, AMARELO, CINZA, LINHA = HexColor("#1a3a5c"), HexColor("#f5a623"), HexColor("#6b7785"), HexColor("#dde3ea")
    CORES = ["#2563eb", "#f59e0b", "#10b981", "#ef4444", "#8b5cf6", "#06b6d4", "#ec4899", "#84cc16", "#f97316", "#64748b"]
    W, H = A4
    M = 34
    e, ano, mes, dias, fl = r["func"], r["ano"], r["mes"], r["dias"], r["folha"] or {}
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"Resumo {e.nome} {MESES[mes - 1]} {ano}")
    c.setAuthor("AZ Elétrica")

    def corta(txt, fonte, tam, larg):
        if stringWidth(txt, fonte, tam) <= larg:
            return txt
        while txt and stringWidth(txt + "…", fonte, tam) > larg:
            txt = txt[:-1]
        return txt + "…"

    def linhas_txt(txt, fonte, tam, larg, maxl):
        palavras, out, cur = txt.split(), [], ""
        for p in palavras:
            t = (cur + " " + p).strip()
            if stringWidth(t, fonte, tam) <= larg:
                cur = t
            else:
                if cur:
                    out.append(cur)
                cur = p
        if cur:
            out.append(cur)
        if len(out) > maxl:
            out = out[:maxl]
            out[-1] = corta(out[-1] + " …", fonte, tam, larg)
        return [corta(x, fonte, tam, larg) for x in out]

    def cabecalho(pag):
        c.setFillColor(AZUL)
        c.rect(0, H - 64, W, 64, stroke=0, fill=1)
        c.setFillColor(AMARELO)
        c.setFont("Helvetica-Bold", 22)
        c.drawString(M, H - 40, "AZ")
        c.setFont("Helvetica", 12)
        c.drawString(M + 34, H - 40, "Elétrica")
        c.setFillColor(white)
        c.setFont("Helvetica", 9)
        c.drawString(M, H - 55, "Resumo mensal de diárias e pagamento")
        c.setFont("Helvetica-Bold", 14)
        c.drawRightString(W - M, H - 38, f"{MESES[mes - 1]} de {ano}")
        c.setFont("Helvetica", 9)
        c.drawRightString(W - M, H - 54, f"Página {pag}")

    def rodape():
        c.setFillColor(CINZA)
        c.setFont("Helvetica", 7.5)
        agora = datetime.now(FORTALEZA).strftime("%d/%m/%Y %H:%M")
        c.drawString(M, 20, f"Gerado em {agora} · AZ Elétrica")
        c.drawRightString(W - M, 20, "Valores conforme apontamentos lançados no sistema")

    pag = 1
    cabecalho(pag)
    y = H - 92
    c.setFillColor(AZUL)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(M, y, e.nome)
    c.setFillColor(CINZA)
    c.setFont("Helvetica", 9.5)
    sub = f"{e.funcao or ''}"
    if e.tipo_contrato == "Mensalista":
        sub += f" · Mensalista · fixo {_brl(e.salario_fixo or 0)}"
    else:
        sub += f" · Diária {_brl(e.valor_diaria or 0)}"
    c.drawString(M, y - 14, sub.strip(" ·"))

    # Quadros de resumo
    tot_diarias = sum(d["diarias"] or 0 for d in dias)
    he_h = sum(d["he_horas"] for d in dias)
    he_v = sum(d["he_valor"] for d in dias)
    quadros = [
        ("DIAS TRABALHADOS", str(len(dias)), ""),
        ("DIÁRIAS", _qtd(tot_diarias) if e.valor_diaria else "—", _brl(sum(d["total"] for d in dias) - he_v - sum(d["adicionais"] for d in dias)) if e.valor_diaria else ""),
        ("HORAS EXTRAS", (_qtd(he_h) + " h") if he_h else ("—" if not he_v else ""), _brl(he_v) if he_v else ""),
        ("A RECEBER", _brl(fl.get("a_receber", 0)) if fl else "—", ""),
    ]
    y -= 30
    gap = 8
    qw = (W - 2 * M - 3 * gap) / 4
    for i, (rot, val, sub2) in enumerate(quadros):
        x = M + i * (qw + gap)
        destaque = i == 3
        c.setFillColor(AZUL if destaque else HexColor("#f5f7fa"))
        c.roundRect(x, y - 50, qw, 50, 6, stroke=0, fill=1)
        c.setFillColor(AMARELO if destaque else CINZA)
        c.setFont("Helvetica-Bold", 7)
        c.drawString(x + 9, y - 13, rot)
        c.setFillColor(white if destaque else AZUL)
        c.setFont("Helvetica-Bold", 14 if len(val) < 14 else 12)
        c.drawString(x + 9, y - 31, val)
        if sub2:
            c.setFont("Helvetica", 7.5)
            c.setFillColor(HexColor("#cbd5e1") if destaque else CINZA)
            c.drawString(x + 9, y - 43, sub2)
    y -= 66

    # Cores por obra (maior gasto primeiro)
    tot_obra, dias_obra = {}, {}
    for d in dias:
        for it in d["itens"]:
            tot_obra[it["obra"]] = tot_obra.get(it["obra"], 0) + it["valor"]
            dias_obra[it["obra"]] = dias_obra.get(it["obra"], 0) + 1
    obras = sorted(tot_obra, key=lambda o: -tot_obra[o])
    cor = {o: HexColor(CORES[i % len(CORES)]) for i, o in enumerate(obras)}

    # Calendário
    c.setFillColor(AZUL)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(M, y, "CALENDÁRIO DE TRABALHO")
    y -= 8
    sem = ["DOM", "SEG", "TER", "QUA", "QUI", "SEX", "SÁB"]
    cw = (W - 2 * M) / 7
    c.setFont("Helvetica-Bold", 7)
    c.setFillColor(CINZA)
    for i, s in enumerate(sem):
        c.drawCentredString(M + i * cw + cw / 2, y - 10, s)
    y -= 15
    por_dia = {d["data"].day: d for d in dias}
    primeiro = (date(ano, mes, 1).weekday() + 1) % 7      # 0 = domingo
    ndias = calendar.monthrange(ano, mes)[1]
    semanas = (primeiro + ndias + 6) // 7
    ch = 44
    for dnum in range(1, ndias + 1):
        pos = primeiro + dnum - 1
        col, lin = pos % 7, pos // 7
        x0, y0 = M + col * cw + 1.5, y - (lin + 1) * ch + 1.5
        w0, h0 = cw - 3, ch - 3
        d = por_dia.get(dnum)
        if d:
            principal = cor[d["itens"][0]["obra"]] if d["itens"] else AZUL
            c.setFillColor(principal)
            c.setFillAlpha(0.13)
            c.roundRect(x0, y0, w0, h0, 4, stroke=0, fill=1)
            c.setFillAlpha(1)
            c.setStrokeColor(principal)
            c.setLineWidth(0.8)
            c.roundRect(x0, y0, w0, h0, 4, stroke=1, fill=0)
            n = len(d["itens"])
            for k, it in enumerate(d["itens"]):
                c.setFillColor(cor[it["obra"]])
                c.rect(x0 + k * w0 / n, y0, w0 / n, 3, stroke=0, fill=1)
            c.setFillColor(AZUL)
            c.setFont("Helvetica-Bold", 8.5)
            c.drawString(x0 + 4, y0 + h0 - 10, str(dnum))
            c.setFont("Helvetica-Bold", 7.5)
            c.drawRightString(x0 + w0 - 4, y0 + h0 - 10, _brl(d["total"]).replace("R$ ", "").replace(",00", ""))
            nome = d["itens"][0]["obra"] if n == 1 else f"{n} obras"
            c.setFont("Helvetica", 6.3)
            c.setFillColor(HexColor("#334155"))
            for k, ln in enumerate(linhas_txt(nome, "Helvetica", 6.3, w0 - 8, 2)):
                c.drawString(x0 + 4, y0 + h0 - 20 - k * 7.5, ln)
            if d["he_horas"] or d["he_valor"]:
                c.setFont("Helvetica-Bold", 6)
                c.setFillColor(HexColor("#b45309"))
                c.drawString(x0 + 4, y0 + 6, f"+{_qtd(d['he_horas'])}h extra" if d["he_horas"] else "+ HE")
        else:
            c.setStrokeColor(LINHA)
            c.setLineWidth(0.5)
            fds = col in (0, 6)
            c.setFillColor(HexColor("#f8fafc") if fds else white)
            c.roundRect(x0, y0, w0, h0, 4, stroke=1, fill=1)
            c.setFillColor(HexColor("#94a3b8"))
            c.setFont("Helvetica", 8)
            c.drawString(x0 + 4, y0 + h0 - 10, str(dnum))
    y -= semanas * ch + 14

    # Legenda das obras
    if obras:
        c.setFont("Helvetica", 8)
        colw = (W - 2 * M) / 2
        for i, o in enumerate(obras):
            xx = M + (i % 2) * colw
            yy = y - (i // 2) * 12
            c.setFillColor(cor[o])
            c.rect(xx, yy - 1, 7, 7, stroke=0, fill=1)
            c.setFillColor(AZUL)
            c.drawString(xx + 11, yy, corta(o, "Helvetica", 8, colw - 120))
            c.setFillColor(CINZA)
            c.drawRightString(xx + colw - 12, yy, f"{dias_obra[o]} dia(s) · {_brl(tot_obra[o])}")
        y -= ((len(obras) + 1) // 2) * 12 + 10

    # Tabela dia a dia
    cols = [("DATA", 52), ("DIA", 30), ("OBRA(S)", 0), ("DIÁRIAS", 46), ("H. EXTRA", 60), ("VALOR", 64)]
    larg_obra = (W - 2 * M) - sum(w for _, w in cols)
    cols[2] = ("OBRA(S)", larg_obra)

    def cab_tabela(yy):
        c.setFillColor(AZUL)
        c.rect(M, yy - 15, W - 2 * M, 15, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 7)
        xx = M
        for i, (t, w) in enumerate(cols):
            if i >= 3:
                c.drawRightString(xx + w - 5, yy - 10.5, t)
            else:
                c.drawString(xx + 5, yy - 10.5, t)
            xx += w
        return yy - 15

    def nova_pagina():
        nonlocal pag
        rodape()
        c.showPage()
        pag += 1
        cabecalho(pag)
        return H - 84

    if y < 140:
        y = nova_pagina()
    c.setFillColor(AZUL)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(M, y, "DETALHE DOS DIAS")
    y = cab_tabela(y - 6)
    sems = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
    for idx, d in enumerate(dias):
        obras_txt = " + ".join(f"{it['obra']}" for it in d["itens"])
        if d["adicionais"]:
            obras_txt += f"  (+ adicional {_brl(d['adicionais'])})"
        lns = linhas_txt(obras_txt, "Helvetica", 7.5, larg_obra - 10, 2)
        rh = 13 if len(lns) == 1 else 21
        if y - rh < 120:
            y = cab_tabela(nova_pagina())
        if idx % 2:
            c.setFillColor(HexColor("#f8fafc"))
            c.rect(M, y - rh, W - 2 * M, rh, stroke=0, fill=1)
        c.setFillColor(HexColor("#1a1a2e"))
        c.setFont("Helvetica", 7.5)
        xx = M
        base = y - 9
        c.drawString(xx + 5, base, d["data"].strftime("%d/%m"))
        xx += cols[0][1]
        c.drawString(xx + 5, base, sems[d["data"].weekday()])
        xx += cols[1][1]
        for k, ln in enumerate(lns):
            c.setFillColor(cor.get(d["itens"][0]["obra"], AZUL) if d["itens"] else AZUL)
            if k == 0:
                c.rect(xx + 5, base - 0.5, 4, 4, stroke=0, fill=1)
            c.setFillColor(HexColor("#1a1a2e"))
            c.drawString(xx + 12, base - k * 8, ln)
        xx += cols[2][1]
        c.drawRightString(xx + cols[3][1] - 5, base, _qtd(d["diarias"]) if d["diarias"] is not None else "—")
        xx += cols[3][1]
        he = (f"{_qtd(d['he_horas'])}h · " if d["he_horas"] else "") + (_brl(d["he_valor"]).replace("R$ ", "") if d["he_valor"] else "")
        c.drawRightString(xx + cols[4][1] - 5, base, he or "—")
        xx += cols[4][1]
        c.setFont("Helvetica-Bold", 7.5)
        c.drawRightString(xx + cols[5][1] - 5, base, _brl(d["total"]))
        c.setStrokeColor(LINHA)
        c.setLineWidth(0.4)
        c.line(M, y - rh, W - M, y - rh)
        y -= rh
    if not dias:
        c.setFillColor(CINZA)
        c.setFont("Helvetica-Oblique", 9)
        c.drawString(M + 5, y - 14, "Nenhum dia trabalhado neste mês.")
        y -= 20
    # total da tabela
    c.setFillColor(HexColor("#eef2f7"))
    c.rect(M, y - 15, W - 2 * M, 15, stroke=0, fill=1)
    c.setFillColor(AZUL)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(M + 5, y - 10.5, "TOTAL DO MÊS")
    c.drawRightString(W - M - 5 - 64 - 60, y - 10.5, _qtd(tot_diarias) if e.valor_diaria else "—")
    c.drawRightString(W - M - 5 - 64, y - 10.5, (f"{_qtd(he_h)}h · " if he_h else "") + (_brl(he_v).replace("R$ ", "") if he_v else "—"))
    c.drawRightString(W - M - 5, y - 10.5, _brl(sum(d["total"] for d in dias)))
    y -= 30

    # Pagamento
    if y < 150:
        y = nova_pagina()
    c.setFillColor(AZUL)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(M, y, "PAGAMENTO")
    y -= 6
    linhas_pg = []
    if fl:
        if e.tipo_contrato == "Mensalista":
            linhas_pg.append(("Salário fixo", fl.get("salario_fixo", 0), ""))
        else:
            linhas_pg.append(("Diárias e horas extras", fl.get("valor_diarias", 0), ""))
        if fl.get("adicionais"):
            linhas_pg.append(("Adicionais", fl["adicionais"], "+"))
        if fl.get("adiantamentos"):
            linhas_pg.append(("Adiantamentos", fl["adiantamentos"], "-"))
        if fl.get("descontos"):
            linhas_pg.append(("Descontos", fl["descontos"], "-"))
        if fl.get("parcelas"):
            det = ", ".join(x.split(":")[0] for x in fl.get("parcelas_detalhe", []))
            linhas_pg.append((f"Parcela de empréstimo ({det})" if det else "Parcelas", fl["parcelas"], "-"))
    bx_w = (W - 2 * M) * 0.55
    for rot, val, sinal in linhas_pg:
        y -= 14
        c.setFillColor(HexColor("#334155"))
        c.setFont("Helvetica", 9)
        c.drawString(M, y, rot)
        c.setFillColor(HexColor("#b91c1c") if sinal == "-" else HexColor("#1a1a2e"))
        c.drawRightString(M + bx_w, y, ("− " if sinal == "-" else "+ " if sinal == "+" else "") + _brl(val))
    y -= 8
    c.setFillColor(AZUL)
    c.roundRect(M, y - 26, bx_w, 24, 5, stroke=0, fill=1)
    c.setFillColor(AMARELO)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(M + 10, y - 17.5, "VALOR A RECEBER")
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 13)
    c.drawRightString(M + bx_w - 10, y - 18, _brl(fl.get("a_receber", 0)) if fl else "—")

    # Comprovantes (lado direito)
    xx = M + bx_w + 18
    yy = y + 14 * len(linhas_pg) + 8
    c.setFillColor(AZUL)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(xx, yy, "COMPROVANTE(S)")
    c.setFont("Helvetica", 8)
    comps = r["comprovantes"]
    if comps:
        for k, cp in enumerate(comps[:6]):
            yy -= 12
            c.setFillColor(HexColor("#15803d"))
            txt = "✓ " if False else "• "
            info = (cp.data_pagamento.strftime("%d/%m/%Y") if cp.data_pagamento else cp.criado_em.strftime("%d/%m/%Y")) + \
                   (f" · {_brl(cp.valor)}" if cp.valor else "")
            c.drawString(xx, yy, corta(txt + info, "Helvetica", 8, W - M - xx))
    else:
        yy -= 12
        c.setFillColor(CINZA)
        c.drawString(xx, yy, "Nenhum anexado")

    rodape()
    c.save()
    return buf.getvalue()


@router.get("/relatorios/funcionario/{eid}/pdf")
def pdf_funcionario(eid: int, ano: int, mes: int = Query(..., ge=1, le=12),
                    db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    r = resumo_mes(db, eid, ano, mes)
    pdf = gerar_pdf(r)
    nome = _ascii(r["func"].nome).replace(" ", "_")
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="Resumo_{nome}_{mes:02d}-{ano}.pdf"'})
