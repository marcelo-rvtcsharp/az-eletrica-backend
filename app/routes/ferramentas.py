"""
Controle de ferramentas: quantas unidades existem e onde está cada uma.

- Cada ferramenta tem uma quantidade total (ex: 3 escadas de 7 degraus).
- FerramentaLocal guarda quantas estão em cada obra.
- O restante (total - o que está nas obras) fica no Depósito.
- Toda movimentação fica registrada no histórico.
- "Último lugar visto": registro rápido (com foto opcional) de onde a ferramenta
  foi vista por último. Serve para achar a ferramenta; não altera as quantidades.
"""
from datetime import date
from typing import Optional

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import get_usuario_atual
from app.models import Ferramenta, FerramentaLocal, FerramentaMov, FerramentaVisto, Obra, get_db

router = APIRouter(prefix="/ferramentas", tags=["ferramentas"])

DEPOSITO = "Depósito"
MAX_FOTO = 8 * 1024 * 1024
FOTOS_OK = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
FORTALEZA = timezone(timedelta(hours=-3))

# colunas do "visto" sem a foto (a foto só é lida quando alguém abre)
_V_COLS = (FerramentaVisto.id, FerramentaVisto.ferramenta_id, FerramentaVisto.data, FerramentaVisto.local,
           FerramentaVisto.obra_id, FerramentaVisto.observacao, FerramentaVisto.registrado_por,
           FerramentaVisto.criado_em, FerramentaVisto.foto_mime)


def _ser_visto(v) -> dict:
    return {"id": v.id, "ferramenta_id": v.ferramenta_id, "data": v.data.isoformat(), "local": v.local,
            "obra_id": v.obra_id, "observacao": v.observacao, "registrado_por": v.registrado_por,
            "tem_foto": bool(v.foto_mime), "criado_em": v.criado_em.isoformat() + "Z" if v.criado_em else None}


def _obras(db: Session) -> dict:
    return dict(db.query(Obra.id, Obra.nome).all())


def _ultimos_vistos(db: Session, ids=None) -> dict:
    sub = db.query(func.max(FerramentaVisto.id)).group_by(FerramentaVisto.ferramenta_id)
    if ids is not None:
        sub = sub.filter(FerramentaVisto.ferramenta_id.in_(ids))
    return {v.ferramenta_id: _ser_visto(v) for v in db.query(*_V_COLS).filter(FerramentaVisto.id.in_(sub)).all()}


def _ser(f: Ferramenta, locais: list, obras: dict, visto=None) -> dict:
    nas_obras = sum(l.quantidade for l in locais)
    return {
        "ultimo_visto": visto,
        "id": f.id,
        "nome": f.nome,
        "quantidade_total": f.quantidade_total,
        "observacao": f.observacao,
        "deposito": f.quantidade_total - nas_obras,
        "em_obras": nas_obras,
        "locais": sorted(
            [{"obra_id": l.obra_id, "obra_nome": obras.get(l.obra_id, "?"), "quantidade": l.quantidade,
              "desde": l.desde.isoformat() if l.desde else None} for l in locais],
            key=lambda x: x["obra_nome"]),
    }


@router.get("/")
def listar(db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    obras = _obras(db)
    por_ferr = {}
    for l in db.query(FerramentaLocal).filter(FerramentaLocal.quantidade > 0).all():
        por_ferr.setdefault(l.ferramenta_id, []).append(l)
    vistos = _ultimos_vistos(db)
    return [_ser(f, por_ferr.get(f.id, []), obras, vistos.get(f.id))
            for f in db.query(Ferramenta).order_by(Ferramenta.nome).all()]


class FerramentaSchema(BaseModel):
    nome: str
    quantidade_total: int = 1
    observacao: Optional[str] = None


@router.post("/")
def criar(data: FerramentaSchema, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    if not data.nome.strip() or data.quantidade_total < 1:
        raise HTTPException(400, "Informe o nome e uma quantidade de pelo menos 1")
    f = Ferramenta(nome=data.nome.strip(), quantidade_total=data.quantidade_total, observacao=data.observacao)
    db.add(f)
    db.flush()
    db.add(FerramentaMov(ferramenta_id=f.id, data=date.today(), quantidade=f.quantidade_total,
                         origem_nome="Cadastro", destino_nome=DEPOSITO, observacao="Ferramenta cadastrada"))
    db.commit()
    db.refresh(f)
    return _ser(f, [], _obras(db))


@router.put("/{id}")
def editar(id: int, data: FerramentaSchema, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    f = db.get(Ferramenta, id)
    if not f:
        raise HTTPException(404, "Ferramenta não encontrada")
    locais = db.query(FerramentaLocal).filter(FerramentaLocal.ferramenta_id == id, FerramentaLocal.quantidade > 0).all()
    nas_obras = sum(l.quantidade for l in locais)
    if data.quantidade_total < max(1, nas_obras):
        raise HTTPException(400, f"Há {nas_obras} unidade(s) em obras. Devolva ao depósito antes de reduzir a quantidade.")
    if data.quantidade_total != f.quantidade_total:
        dif = data.quantidade_total - f.quantidade_total
        db.add(FerramentaMov(ferramenta_id=id, data=date.today(), quantidade=abs(dif),
                             origem_nome="Cadastro" if dif > 0 else DEPOSITO,
                             destino_nome=DEPOSITO if dif > 0 else "Baixa",
                             observacao="Quantidade aumentada" if dif > 0 else "Quantidade reduzida (baixa)"))
    f.nome, f.quantidade_total, f.observacao = data.nome.strip(), data.quantidade_total, data.observacao
    db.commit()
    return _ser(f, locais, _obras(db), _ultimos_vistos(db, [id]).get(id))


@router.delete("/{id}")
def excluir(id: int, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    f = db.get(Ferramenta, id)
    if not f:
        raise HTTPException(404, "Ferramenta não encontrada")
    db.query(FerramentaLocal).filter(FerramentaLocal.ferramenta_id == id).delete()
    db.query(FerramentaMov).filter(FerramentaMov.ferramenta_id == id).delete()
    db.query(FerramentaVisto).filter(FerramentaVisto.ferramenta_id == id).delete()
    db.delete(f)
    db.commit()
    return {"ok": True}


class MoverSchema(BaseModel):
    quantidade: int
    origem_obra_id: Optional[int] = None    # None = Depósito
    destino_obra_id: Optional[int] = None   # None = Depósito
    data: Optional[date] = None
    observacao: Optional[str] = None


@router.post("/{id}/mover")
def mover(id: int, m: MoverSchema, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    f = db.get(Ferramenta, id)
    if not f:
        raise HTTPException(404, "Ferramenta não encontrada")
    if m.quantidade < 1:
        raise HTTPException(400, "Quantidade deve ser pelo menos 1")
    if m.origem_obra_id == m.destino_obra_id:
        raise HTTPException(400, "Origem e destino são o mesmo lugar")
    obras = _obras(db)
    for oid in (m.origem_obra_id, m.destino_obra_id):
        if oid is not None and oid not in obras:
            raise HTTPException(404, "Obra não encontrada")

    def local(oid):
        return db.query(FerramentaLocal).filter(FerramentaLocal.ferramenta_id == id,
                                                FerramentaLocal.obra_id == oid).first()

    # disponível na origem
    if m.origem_obra_id is None:
        nas_obras = sum(l.quantidade for l in db.query(FerramentaLocal).filter(FerramentaLocal.ferramenta_id == id).all())
        disponivel = f.quantidade_total - nas_obras
    else:
        lo = local(m.origem_obra_id)
        disponivel = lo.quantidade if lo else 0
    if m.quantidade > disponivel:
        origem = DEPOSITO if m.origem_obra_id is None else obras[m.origem_obra_id]
        raise HTTPException(400, f"Só há {disponivel} unidade(s) em {origem}")

    dia = m.data or date.today()
    if m.origem_obra_id is not None:
        lo = local(m.origem_obra_id)
        lo.quantidade -= m.quantidade
        if lo.quantidade <= 0:
            db.delete(lo)
    if m.destino_obra_id is not None:
        ld = local(m.destino_obra_id)
        if ld:
            ld.quantidade += m.quantidade
        else:
            db.add(FerramentaLocal(ferramenta_id=id, obra_id=m.destino_obra_id, quantidade=m.quantidade, desde=dia))
    db.add(FerramentaMov(
        ferramenta_id=id, data=dia, quantidade=m.quantidade,
        origem_nome=DEPOSITO if m.origem_obra_id is None else obras[m.origem_obra_id],
        destino_nome=DEPOSITO if m.destino_obra_id is None else obras[m.destino_obra_id],
        observacao=m.observacao))
    db.commit()
    locais = db.query(FerramentaLocal).filter(FerramentaLocal.ferramenta_id == id, FerramentaLocal.quantidade > 0).all()
    return _ser(f, locais, obras, _ultimos_vistos(db, [id]).get(id))


@router.get("/{id}/historico")
def historico(id: int, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    movs = (db.query(FerramentaMov).filter(FerramentaMov.ferramenta_id == id)
            .order_by(FerramentaMov.data.desc(), FerramentaMov.id.desc()).limit(100).all())
    return [{"id": x.id, "data": x.data.isoformat(), "quantidade": x.quantidade, "origem": x.origem_nome,
             "destino": x.destino_nome, "observacao": x.observacao} for x in movs]


# ── Último lugar visto (com foto) ─────────────────────────────────────────
@router.post("/{id}/visto")
async def registrar_visto(
    id: int, local: Optional[str] = Form(None), obra_id: Optional[int] = Form(None), data: Optional[date] = Form(None),
    observacao: Optional[str] = Form(None), foto: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db), usuario=Depends(get_usuario_atual),
):
    if not db.get(Ferramenta, id):
        raise HTTPException(404, "Ferramenta não encontrada")
    local = (local or "").strip()
    if obra_id is not None:
        obra = db.get(Obra, obra_id)
        if not obra:
            raise HTTPException(404, "Obra não encontrada")
        local = local or obra.nome
    if not local:
        raise HTTPException(400, "Informe onde a ferramenta foi vista")
    dados, mime = None, None
    if foto is not None and foto.filename:
        dados = await foto.read()
        if len(dados) > MAX_FOTO:
            raise HTTPException(400, "Foto maior que 8 MB")
        mime = (foto.content_type or "").lower()
        if mime not in FOTOS_OK:
            raise HTTPException(400, "Envie uma foto (JPG, PNG ou WEBP)")
        if not dados:
            dados, mime = None, None
    v = FerramentaVisto(ferramenta_id=id, data=data or datetime.now(FORTALEZA).date(), local=local[:200],
                        obra_id=obra_id, observacao=(observacao or "").strip()[:500] or None,
                        registrado_por=getattr(usuario, "nome", None), foto=dados, foto_mime=mime)
    db.add(v)
    db.commit()
    db.refresh(v)
    return _ser_visto(v)


@router.get("/{id}/vistos")
def listar_vistos(id: int, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    vs = (db.query(*_V_COLS).filter(FerramentaVisto.ferramenta_id == id)
          .order_by(FerramentaVisto.data.desc(), FerramentaVisto.id.desc()).limit(50).all())
    return [_ser_visto(v) for v in vs]


@router.get("/vistos/{vid}/foto")
def foto_visto(vid: int, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    v = db.get(FerramentaVisto, vid)
    if not v or not v.foto_mime:
        raise HTTPException(404, "Foto não encontrada")
    return Response(content=v.foto, media_type=v.foto_mime, headers={"Cache-Control": "private, max-age=86400"})


@router.delete("/vistos/{vid}")
def excluir_visto(vid: int, db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    v = db.get(FerramentaVisto, vid)
    if not v:
        raise HTTPException(404, "Registro não encontrado")
    db.delete(v)
    db.commit()
    return {"ok": True}


def devolver_ao_deposito(db: Session, obra: Obra):
    """Chamado ao excluir uma obra: as ferramentas que estavam lá voltam ao depósito."""
    for l in db.query(FerramentaLocal).filter(FerramentaLocal.obra_id == obra.id).all():
        db.add(FerramentaMov(ferramenta_id=l.ferramenta_id, data=date.today(), quantidade=l.quantidade,
                             origem_nome=obra.nome, destino_nome=DEPOSITO,
                             observacao="Obra excluída: voltou ao depósito"))
        db.delete(l)
