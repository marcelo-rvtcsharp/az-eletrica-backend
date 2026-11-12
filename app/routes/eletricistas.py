from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import date, datetime
from app.models import Eletricista, Apontamento, LancamentoFinanceiro, Emprestimo, get_db
from app.auth import get_usuario_atual

router = APIRouter(prefix="/eletricistas", tags=["eletricistas"])


class EletricistaSchema(BaseModel):
    nome: str
    telefone: Optional[str] = None
    cpf: Optional[str] = None
    funcao: str = "Eletricista"
    valor_diaria: float
    tipo_contrato: str = "Diarista"
    salario_fixo: float = 0
    nr10_validade: Optional[date] = None
    nr35_validade: Optional[date] = None
    data_entrada: Optional[date] = None
    status: str = "Ativo"


def serializar(e: Eletricista) -> dict:
    return {
        "id": e.id,
        "nome": e.nome,
        "telefone": e.telefone,
        "cpf": e.cpf,
        "funcao": e.funcao,
        "valor_diaria": e.valor_diaria,
        "tipo_contrato": e.tipo_contrato,
        "salario_fixo": e.salario_fixo or 0,
        "nr10_validade": str(e.nr10_validade) if e.nr10_validade else None,
        "nr35_validade": str(e.nr35_validade) if e.nr35_validade else None,
        "data_entrada": str(e.data_entrada) if e.data_entrada else None,
        "status": e.status,
        "criado_em": e.criado_em.isoformat() if e.criado_em else None,
        "atualizadoEm": e.atualizadoEm.isoformat() if e.atualizadoEm else None,
    }


@router.get("/")
def listar(db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    return [serializar(e) for e in db.query(Eletricista).order_by(Eletricista.nome).all()]


@router.post("/")
def criar(data: EletricistaSchema, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    e = Eletricista(**data.model_dump())
    db.add(e)
    db.commit()
    db.refresh(e)
    return serializar(e)


@router.get("/{id}")
def buscar(id: int, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    e = db.query(Eletricista).filter(Eletricista.id == id).first()
    if not e:
        raise HTTPException(404, "Eletricista não encontrado")
    return serializar(e)


@router.put("/{id}")
def atualizar(id: int, data: EletricistaSchema, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    e = db.query(Eletricista).filter(Eletricista.id == id).first()
    if not e:
        raise HTTPException(404, "Eletricista não encontrado")
    for k, v in data.model_dump().items():
        setattr(e, k, v)
    e.atualizadoEm = datetime.utcnow()
    db.commit()
    db.refresh(e)
    return serializar(e)


@router.delete("/{id}")
def deletar(id: int, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    e = db.query(Eletricista).filter(Eletricista.id == id).first()
    if not e:
        raise HTTPException(404, "Eletricista não encontrado")

    # Apaga os apontamentos (cabeçalho) deste eletricista — itens e
    # adicionais são removidos em cascata automaticamente
    apontamentos = db.query(Apontamento).filter(Apontamento.eletricista_id == id).all()
    for a in apontamentos:
        db.delete(a)
    db.flush()

    db.query(LancamentoFinanceiro).filter(LancamentoFinanceiro.eletricista_id == id).delete()
    db.query(Emprestimo).filter(Emprestimo.eletricista_id == id).delete()
    db.delete(e)
    db.commit()
    return {"ok": True}