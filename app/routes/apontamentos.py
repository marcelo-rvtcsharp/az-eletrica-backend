from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from pydantic import BaseModel
from typing import List, Optional
from datetime import date, datetime
from app.models import Apontamento, ApontamentoItem, ApontamentoAdicional, Eletricista, Obra, get_db
from app.auth import get_usuario_atual, Usuario

router = APIRouter(prefix="/apontamentos", tags=["apontamentos"])


class ItemSchema(BaseModel):
    obra_id: int
    valor: float


class AdicionalSchema(BaseModel):
    tipo: str       # sabado_feriado | semana_pesada | hora_extra | outro
    descricao: str  # Ex: "Sábado 28/06", "Hora extra 2h", "Semana pesada"
    valor: float


class ApontamentoSchema(BaseModel):
    eletricista_id: int
    data: date
    observacao: Optional[str] = None
    itens: List[ItemSchema]
    adicionais: Optional[List[AdicionalSchema]] = []


TIPO_LABELS = {
    "sabado_feriado": "Sábado / Feriado",
    "semana_pesada":  "Semana pesada",
    "hora_extra":     "Hora extra",
    "outro":          "Outro adicional",
}


def serializar(a: Apontamento) -> dict:
    total_obras      = round(sum(i.valor for i in a.itens), 2)
    total_adicionais = round(sum(ad.valor for ad in a.adicionais), 2)
    total_dia        = round(total_obras + total_adicionais, 2)

    elet_nome = a.itens[0].eletricista.nome if a.itens else (
        a.adicionais[0].eletricista_id if a.adicionais else ""
    )

    return {
        "id": a.id,
        "eletricista_id": a.eletricista_id,
        "eletricista_nome": elet_nome,
        "data": str(a.data),
        "observacao": a.observacao,
        "criado_em": a.criado_em.isoformat() if a.criado_em else None,
        "total_obras": total_obras,
        "total_adicionais": total_adicionais,
        "total_dia": total_dia,
        "itens": [
            {
                "id": i.id,
                "obra_id": i.obra_id,
                "obra_nome": i.obra.nome if i.obra else "",
                "valor": i.valor,
            }
            for i in a.itens
        ],
        "adicionais": [
            {
                "id": ad.id,
                "tipo": ad.tipo,
                "tipo_label": TIPO_LABELS.get(ad.tipo, ad.tipo),
                "descricao": ad.descricao,
                "valor": ad.valor,
            }
            for ad in a.adicionais
        ],
    }


def _carregar(db: Session, apto_id: int) -> Apontamento:
    return db.query(Apontamento).options(
        joinedload(Apontamento.itens).joinedload(ApontamentoItem.eletricista),
        joinedload(Apontamento.itens).joinedload(ApontamentoItem.obra),
        joinedload(Apontamento.adicionais),
    ).filter(Apontamento.id == apto_id).first()


@router.get("/")
def listar(
    data_inicio:    Optional[date] = None,
    data_fim:       Optional[date] = None,
    eletricista_id: Optional[int]  = None,
    obra_id:        Optional[int]  = None,
    db: Session = Depends(get_db),
    _=Depends(get_usuario_atual)
):
    q = db.query(Apontamento).options(
        joinedload(Apontamento.itens).joinedload(ApontamentoItem.eletricista),
        joinedload(Apontamento.itens).joinedload(ApontamentoItem.obra),
        joinedload(Apontamento.adicionais),
    )
    if data_inicio:    q = q.filter(Apontamento.data >= data_inicio)
    if data_fim:       q = q.filter(Apontamento.data <= data_fim)
    if eletricista_id: q = q.filter(Apontamento.eletricista_id == eletricista_id)
    if obra_id:        q = q.join(ApontamentoItem).filter(ApontamentoItem.obra_id == obra_id)

    return [serializar(a) for a in q.order_by(Apontamento.data.desc()).all()]


@router.post("/")
def criar(
    data: ApontamentoSchema,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(get_usuario_atual)
):
    if not data.itens:
        raise HTTPException(400, "Adicione pelo menos uma obra")

    # Substitui apontamento existente para o mesmo eletricista/dia
    existente = db.query(Apontamento).filter(
        Apontamento.eletricista_id == data.eletricista_id,
        Apontamento.data == data.data
    ).first()
    if existente:
        db.delete(existente)
        db.commit()

    apto = Apontamento(
        eletricista_id=data.eletricista_id,
        data=data.data,
        observacao=data.observacao,
        criado_por=usuario.id,
    )
    db.add(apto)
    db.flush()

    # Obras do dia
    for item in data.itens:
        db.add(ApontamentoItem(
            apontamento_id=apto.id,
            eletricista_id=data.eletricista_id,
            obra_id=item.obra_id,
            valor=item.valor,
        ))

    # Adicionais do dia
    for ad in (data.adicionais or []):
        db.add(ApontamentoAdicional(
            apontamento_id=apto.id,
            eletricista_id=data.eletricista_id,
            tipo=ad.tipo,
            descricao=ad.descricao,
            valor=ad.valor,
        ))

    db.commit()
    return serializar(_carregar(db, apto.id))


@router.get("/{id}")
def buscar(id: int, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    a = _carregar(db, id)
    if not a:
        raise HTTPException(404, "Apontamento não encontrado")
    return serializar(a)


@router.delete("/{id}")
def deletar(id: int, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    a = db.query(Apontamento).filter(Apontamento.id == id).first()
    if not a:
        raise HTTPException(404, "Apontamento não encontrado")
    db.delete(a)
    db.commit()
    return {"ok": True}


# ── Endpoint de adicionais avulsos (adicionar a apontamento existente) ──────
@router.post("/{id}/adicionais")
def adicionar_adicional(
    id: int,
    data: AdicionalSchema,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(get_usuario_atual)
):
    """Adiciona um adicional a um apontamento já existente"""
    a = db.query(Apontamento).filter(Apontamento.id == id).first()
    if not a:
        raise HTTPException(404, "Apontamento não encontrado")

    db.add(ApontamentoAdicional(
        apontamento_id=id,
        eletricista_id=a.eletricista_id,
        tipo=data.tipo,
        descricao=data.descricao,
        valor=data.valor,
    ))
    db.commit()
    return serializar(_carregar(db, id))


@router.delete("/{id}/adicionais/{adicional_id}")
def remover_adicional(
    id: int,
    adicional_id: int,
    db: Session = Depends(get_db),
    _=Depends(get_usuario_atual)
):
    """Remove um adicional específico de um apontamento"""
    ad = db.query(ApontamentoAdicional).filter(
        ApontamentoAdicional.id == adicional_id,
        ApontamentoAdicional.apontamento_id == id
    ).first()
    if not ad:
        raise HTTPException(404, "Adicional não encontrado")
    db.delete(ad)
    db.commit()
    return {"ok": True}
