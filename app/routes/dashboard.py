"""
Dashboard por mês (igual à aba "Dashboard Mensal" da planilha).

GET /dashboard/              -> mês atual (horário de Fortaleza)
GET /dashboard/?ano=2026&mes=10
"""
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import get_usuario_atual
from app.models import Apontamento, ApontamentoItem, Eletricista, Obra, get_db
from app.routes.despesas import total_despesas_mes

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

FORTALEZA = timezone(timedelta(hours=-3))


def _intervalo(ano: int, mes: int):
    return date(ano, mes, 1), date(ano + (mes == 12), mes % 12 + 1, 1)


@router.get("/")
def resumo(
    ano: Optional[int] = None,
    mes: Optional[int] = Query(None, ge=1, le=12),
    db: Session = Depends(get_db),
    _=Depends(get_usuario_atual),
):
    hoje = datetime.now(FORTALEZA).date()
    if not (ano and mes):
        ano, mes = hoje.year, hoje.month
    ini, fim = _intervalo(ano, mes)
    ini_ant, _fim_ant = _intervalo(ano - (mes == 1), (mes - 2) % 12 + 1)

    no_mes = (Apontamento.data >= ini, Apontamento.data < fim)

    # ── Totais do mês ─────────────────────────────────────────
    total_mes = (db.query(func.sum(ApontamentoItem.valor))
                 .join(Apontamento, Apontamento.id == ApontamentoItem.apontamento_id)
                 .filter(*no_mes).scalar() or 0)
    total_ant = (db.query(func.sum(ApontamentoItem.valor))
                 .join(Apontamento, Apontamento.id == ApontamentoItem.apontamento_id)
                 .filter(Apontamento.data >= ini_ant, Apontamento.data < ini).scalar() or 0)
    dias_com_lancamento = db.query(func.count(func.distinct(Apontamento.data))).filter(*no_mes).scalar() or 0
    diarias_lancadas = db.query(func.count(Apontamento.id)).filter(*no_mes).scalar() or 0

    # ── Gasto por obra: no mês e acumulado ────────────────────
    gasto_mes = dict(db.query(ApontamentoItem.obra_id, func.sum(ApontamentoItem.valor))
                     .join(Apontamento, Apontamento.id == ApontamentoItem.apontamento_id)
                     .filter(*no_mes).group_by(ApontamentoItem.obra_id).all())
    gasto_total = dict(db.query(ApontamentoItem.obra_id, func.sum(ApontamentoItem.valor))
                       .group_by(ApontamentoItem.obra_id).all())

    obras_resumo, obras_mes = [], []
    total_contratado = total_gasto = 0.0
    obras_negativas = obras_atencao = obras_sem_contrato = 0
    for o in db.query(Obra).all():
        gasto = float(gasto_total.get(o.id) or 0)
        gm = float(gasto_mes.get(o.id) or 0)
        saldo = o.valor_contratado - gasto
        perc = round(gasto / o.valor_contratado * 100, 1) if o.valor_contratado > 0 else 0
        situacao = "negativo" if saldo < 0 else "atencao" if perc >= o.alerta_percentual else "ok"
        item = {
            "id": o.id, "nome": o.nome, "cliente": o.cliente, "cidade": o.cidade,
            "valor_contratado": o.valor_contratado,
            "total_gasto": round(gasto, 2), "saldo": round(saldo, 2),
            "percentual": perc, "situacao": situacao,
            "gasto_mes": round(gm, 2),
            "perc_do_mes": round(gm / total_mes * 100, 1) if total_mes else 0,
        }
        if gm:
            obras_mes.append(item)
        if o.status == "Concluída":
            continue
        if o.valor_contratado <= 0:
            if gasto > 0:
                obras_sem_contrato += 1
        elif situacao == "negativo":
            obras_negativas += 1
        if situacao == "atencao":
            obras_atencao += 1
        total_contratado += o.valor_contratado
        total_gasto += gasto
        obras_resumo.append(item)

    # ── Gasto por funcionário no mês ──────────────────────────
    func_mes = (db.query(Eletricista.nome, func.sum(ApontamentoItem.valor),
                         func.count(func.distinct(Apontamento.data)))
                .join(Apontamento, Apontamento.eletricista_id == Eletricista.id)
                .join(ApontamentoItem, ApontamentoItem.apontamento_id == Apontamento.id)
                .filter(*no_mes).group_by(Eletricista.nome).all())
    funcionarios_mes = sorted(
        [{"nome": n, "valor": round(float(v or 0), 2), "dias": int(d or 0),
          "perc_do_mes": round(float(v or 0) / total_mes * 100, 1) if total_mes else 0}
         for n, v, d in func_mes],
        key=lambda x: x["valor"], reverse=True)

    # ── Últimos apontamentos do mês ───────────────────────────
    linhas = (db.query(Apontamento.data, Eletricista.nome, Obra.nome, ApontamentoItem.valor)
              .join(ApontamentoItem, ApontamentoItem.apontamento_id == Apontamento.id)
              .outerjoin(Eletricista, Eletricista.id == Apontamento.eletricista_id)
              .outerjoin(Obra, Obra.id == ApontamentoItem.obra_id)
              .filter(*no_mes)
              .order_by(Apontamento.data.desc(), Apontamento.criado_em.desc(), Apontamento.id.desc())
              .limit(12).all())
    ultimos = [{"eletricista": en or "", "obra": on or "", "valor": v, "data": str(d)} for d, en, on, v in linhas]

    return {
        "periodo": {"ano": ano, "mes": mes, "mes_atual": (ano, mes) == (hoje.year, hoje.month)},
        "mes": {
            "total_diarias": round(float(total_mes), 2),
            "total_mes_anterior": round(float(total_ant), 2),
            "dias_com_lancamento": int(dias_com_lancamento),
            "lancamentos": int(diarias_lancadas),
            "funcionarios": len(funcionarios_mes),
            "obras_com_lancamento": len(obras_mes),
        },
        "custos": (lambda dsp: {
            "mao_de_obra": round(float(total_mes), 2),
            "alimentacao": dsp["alimentacao"],
            "outros": dsp["outros"],
            "total": round(float(total_mes) + dsp["total"], 2),
        })(total_despesas_mes(db, ano, mes)),
        "obras_mes": sorted(obras_mes, key=lambda x: x["gasto_mes"], reverse=True),
        "funcionarios_mes": funcionarios_mes,
        "obras": {
            "ativas": len(obras_resumo),
            "negativas": obras_negativas,
            "atencao": obras_atencao,
            "sem_contrato": obras_sem_contrato,
            "total_contratado": round(total_contratado, 2),
            "total_gasto": round(total_gasto, 2),
            "saldo_geral": round(total_contratado - total_gasto, 2),
            "lista": sorted(obras_resumo, key=lambda x: x["percentual"], reverse=True),
        },
        "equipe": {"ativos": db.query(Eletricista).filter(Eletricista.status == "Ativo").count()},
        "ultimos_apontamentos": ultimos,
    }
