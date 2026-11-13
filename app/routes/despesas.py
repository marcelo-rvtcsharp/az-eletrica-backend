"""
Despesas do mês: alimentação, combustível/frete, ferramentas/EPI, hospedagem, outros.

A alimentação pode ser calculada automaticamente, igual à aba "Alimentação"
da planilha: em cada dia com apontamento, conta as pessoas que trabalharam
(+ mensalistas ativos) e multiplica pelo valor por pessoa/dia.
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import get_usuario_atual
from app.models import Apontamento, Despesa, Eletricista, Obra, get_db

router = APIRouter(prefix="/despesas", tags=["despesas"])

CATEGORIAS = {
    "alimentacao": "Alimentação",
    "combustivel": "Combustível / Frete",
    "ferramentas": "Ferramentas / EPI",
    "hospedagem": "Hospedagem / Deslocamento",
    "outros": "Outros",
}


def _intervalo(ano: int, mes: int):
    return date(ano, mes, 1), date(ano + (mes == 12), mes % 12 + 1, 1)


def _ser(d: Despesa, obras: dict) -> dict:
    return {
        "id": d.id, "data": d.data.isoformat(), "categoria": d.categoria,
        "categoria_nome": CATEGORIAS.get(d.categoria, d.categoria),
        "valor": d.valor, "descricao": d.descricao, "pessoas": d.pessoas,
        "obra_id": d.obra_id, "obra_nome": obras.get(d.obra_id) if d.obra_id else None,
        "automatica": bool(d.automatica),
    }


def _obras(db: Session) -> dict:
    return dict(db.query(Obra.id, Obra.nome).all())


class DespesaSchema(BaseModel):
    data: date
    categoria: str
    valor: float
    descricao: Optional[str] = None
    pessoas: Optional[int] = None
    obra_id: Optional[int] = None
    automatica: bool = False      # usado pela importação da planilha


def _validar(data: DespesaSchema):
    if data.categoria not in CATEGORIAS:
        raise HTTPException(400, "Categoria inválida")
    if data.valor <= 0:
        raise HTTPException(400, "Informe um valor maior que zero")


@router.get("/categorias")
def categorias(_=Depends(get_usuario_atual)):
    return [{"id": k, "nome": v} for k, v in CATEGORIAS.items()]


@router.get("/")
def listar(
    ano: int, mes: int = Query(..., ge=1, le=12),
    db: Session = Depends(get_db), _=Depends(get_usuario_atual),
):
    ini, fim = _intervalo(ano, mes)
    ds = (db.query(Despesa).filter(Despesa.data >= ini, Despesa.data < fim)
          .order_by(Despesa.data.desc(), Despesa.id.desc()).all())
    obras = _obras(db)
    por_cat = {k: 0.0 for k in CATEGORIAS}
    for d in ds:
        por_cat[d.categoria] = por_cat.get(d.categoria, 0.0) + d.valor
    alim = [d for d in ds if d.categoria == "alimentacao"]
    return {
        "despesas": [_ser(d, obras) for d in ds],
        "por_categoria": [{"id": k, "nome": CATEGORIAS.get(k, k), "valor": round(v, 2)} for k, v in por_cat.items()],
        "total": round(sum(por_cat.values()), 2),
        "alimentacao": {
            "dias": len({d.data for d in alim}),
            "refeicoes": sum(d.pessoas or 0 for d in alim),
            "total": round(por_cat["alimentacao"], 2),
        },
    }


@router.post("/")
def criar(data: DespesaSchema, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    _validar(data)
    d = Despesa(**data.model_dump())
    db.add(d)
    db.commit()
    db.refresh(d)
    return _ser(d, _obras(db))


@router.put("/{id}")
def editar(id: int, data: DespesaSchema, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    d = db.get(Despesa, id)
    if not d:
        raise HTTPException(404, "Despesa não encontrada")
    _validar(data)
    for k, v in data.model_dump(exclude={"automatica"}).items():
        setattr(d, k, v)
    d.automatica = False          # editada à mão: o recálculo automático não mexe mais nela
    db.commit()
    db.refresh(d)
    return _ser(d, _obras(db))


@router.delete("/{id}")
def excluir(id: int, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    d = db.get(Despesa, id)
    if not d:
        raise HTTPException(404, "Despesa não encontrada")
    db.delete(d)
    db.commit()
    return {"ok": True}


class CalculoAlimentacao(BaseModel):
    ano: int
    mes: int
    valor_pessoa: float = 20.0
    incluir_mensalistas: bool = True


@router.post("/alimentacao/calcular")
def calcular_alimentacao(data: CalculoAlimentacao, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    """Recalcula a alimentação do mês pelos apontamentos.
    Substitui só os dias calculados automaticamente; dias lançados/editados à mão ficam."""
    if not 1 <= data.mes <= 12 or data.valor_pessoa <= 0:
        raise HTTPException(400, "Mês ou valor por pessoa inválido")
    ini, fim = _intervalo(data.ano, data.mes)

    pessoas_dia = {}
    for dia, eid in (db.query(Apontamento.data, Apontamento.eletricista_id)
                     .filter(Apontamento.data >= ini, Apontamento.data < fim).distinct().all()):
        pessoas_dia.setdefault(dia, set()).add(eid)

    mensalistas = []
    if data.incluir_mensalistas:
        mensalistas = [e.id for e in db.query(Eletricista).filter(
            Eletricista.tipo_contrato == "Mensalista", Eletricista.status == "Ativo").all()]

    manuais = {d for (d,) in db.query(Despesa.data).filter(
        Despesa.categoria == "alimentacao", Despesa.automatica == False,  # noqa: E712
        Despesa.data >= ini, Despesa.data < fim).all()}

    db.query(Despesa).filter(
        Despesa.categoria == "alimentacao", Despesa.automatica == True,  # noqa: E712
        Despesa.data >= ini, Despesa.data < fim).delete()

    total = 0.0
    dias = 0
    for dia in sorted(pessoas_dia):
        if dia in manuais:
            continue
        n = len(pessoas_dia[dia] | set(mensalistas))
        valor = round(n * data.valor_pessoa, 2)
        db.add(Despesa(data=dia, categoria="alimentacao", valor=valor, pessoas=n,
                       descricao=f"{n} pessoa(s) × R$ " + f"{data.valor_pessoa:.2f}".replace(".", ","), automatica=True))
        total += valor
        dias += 1
    db.commit()
    return {"dias": dias, "dias_manuais_mantidos": len(manuais), "total": round(total, 2)}


def total_despesas_mes(db: Session, ano: int, mes: int) -> dict:
    """Usado pelo dashboard."""
    ini, fim = _intervalo(ano, mes)
    linhas = (db.query(Despesa.categoria, func.sum(Despesa.valor))
              .filter(Despesa.data >= ini, Despesa.data < fim)
              .group_by(Despesa.categoria).all())
    por_cat = {k: round(float(v or 0), 2) for k, v in linhas}
    return {"alimentacao": por_cat.get("alimentacao", 0.0),
            "outros": round(sum(v for k, v in por_cat.items() if k != "alimentacao"), 2),
            "total": round(sum(por_cat.values()), 2)}
