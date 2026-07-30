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
        db.add(OrcamentoItem(orcamento_id=orc.id, ordem=idx, **item.model_dump()))
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
        db.add(OrcamentoItem(orcamento_id=orc.id, ordem=idx, **item.model_dump()))
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
