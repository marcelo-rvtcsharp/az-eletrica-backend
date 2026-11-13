from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel
from typing import Optional
from datetime import date, datetime
from app.models import Obra, Apontamento, ApontamentoItem, Despesa, get_db
from app.auth import get_usuario_atual

router = APIRouter(prefix="/obras", tags=["obras"])


class ObraSchema(BaseModel):
    nome: str
    cliente: str
    contato: Optional[str] = None
    cidade: Optional[str] = None
    bairro: Optional[str] = None
    endereco: Optional[str] = None
    tipo_servico: str = "Elétrica residencial"
    valor_contratado: float
    data_inicio: Optional[date] = None
    data_previsao: Optional[date] = None
    data_conclusao: Optional[date] = None
    status: str = "Em andamento"
    alerta_percentual: int = 85
    observacoes: Optional[str] = None


def serializar(o: Obra, total_gasto: float = 0) -> dict:
    saldo = o.valor_contratado - total_gasto
    percentual = round((total_gasto / o.valor_contratado * 100), 1) if o.valor_contratado > 0 else 0
    return {
        "id": o.id,
        "nome": o.nome,
        "cliente": o.cliente,
        "contato": o.contato,
        "cidade": o.cidade,
        "bairro": o.bairro,
        "endereco": o.endereco,
        "tipo_servico": o.tipo_servico,
        "valor_contratado": o.valor_contratado,
        "total_gasto": round(total_gasto, 2),
        "saldo": round(saldo, 2),
        "percentual_consumido": percentual,
        "situacao": "negativo" if saldo < 0 else "atencao" if percentual >= o.alerta_percentual else "ok",
        "data_inicio": str(o.data_inicio) if o.data_inicio else None,
        "data_previsao": str(o.data_previsao) if o.data_previsao else None,
        "data_conclusao": str(o.data_conclusao) if o.data_conclusao else None,
        "status": o.status,
        "alerta_percentual": o.alerta_percentual,
        "observacoes": o.observacoes,
        "criado_em": o.criado_em.isoformat() if o.criado_em else None,
        "atualizadoEm": o.atualizadoEm.isoformat() if o.atualizadoEm else None,
    }


def get_total_gasto(db: Session, obra_id: int) -> float:
    result = db.query(func.sum(ApontamentoItem.valor)).filter(ApontamentoItem.obra_id == obra_id).scalar()
    return result or 0


@router.get("/")
def listar(db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    obras = db.query(Obra).order_by(Obra.criado_em.desc()).all()
    # um único SELECT ... GROUP BY em vez de uma consulta por obra
    gastos = dict(db.query(ApontamentoItem.obra_id, func.sum(ApontamentoItem.valor))
                  .group_by(ApontamentoItem.obra_id).all())
    return [serializar(o, gastos.get(o.id) or 0) for o in obras]


@router.post("/")
def criar(data: ObraSchema, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    o = Obra(**data.model_dump())
    db.add(o)
    db.commit()
    db.refresh(o)
    return serializar(o)


@router.get("/{id}")
def buscar(id: int, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    o = db.query(Obra).filter(Obra.id == id).first()
    if not o:
        raise HTTPException(404, "Obra não encontrada")
    return serializar(o, get_total_gasto(db, id))


@router.put("/{id}")
def atualizar(id: int, data: ObraSchema, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    o = db.query(Obra).filter(Obra.id == id).first()
    if not o:
        raise HTTPException(404, "Obra não encontrada")
    for k, v in data.model_dump().items():
        setattr(o, k, v)
    o.atualizadoEm = datetime.utcnow()
    db.commit()
    db.refresh(o)
    return serializar(o, get_total_gasto(db, id))


@router.delete("/{id}")
def deletar(id: int, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    o = db.query(Obra).filter(Obra.id == id).first()
    if not o:
        raise HTTPException(404, "Obra não encontrada")

    # Apaga os itens de apontamento que referenciam esta obra
    itens = db.query(ApontamentoItem).filter(ApontamentoItem.obra_id == id).all()
    apontamento_ids = {i.apontamento_id for i in itens}
    for i in itens:
        db.delete(i)
    db.flush()

    # Se algum apontamento ficou sem itens e sem adicionais, remove o cabeçalho também
    for apto_id in apontamento_ids:
        apto = db.query(Apontamento).filter(Apontamento.id == apto_id).first()
        if apto and not apto.itens and not apto.adicionais:
            db.delete(apto)

    db.query(Despesa).filter(Despesa.obra_id == id).update({Despesa.obra_id: None})
    from app.routes.ferramentas import devolver_ao_deposito
    devolver_ao_deposito(db, o)
    db.delete(o)
    db.commit()
    return {"ok": True}