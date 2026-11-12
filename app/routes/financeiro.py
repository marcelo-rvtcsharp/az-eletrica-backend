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
