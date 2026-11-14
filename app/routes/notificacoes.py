"""
Notificações para o escritório (CEO e engenheiro):
- ficam salvas no banco (sininho do app);
- e, se o celular autorizou, chegam como notificação push (Web Push / VAPID).
As chaves VAPID são geradas uma única vez e guardadas na tabela config.
"""
import base64
import json
import threading
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import exigir_admin
from app.models import Config, Notificacao, PushAssinatura, SessionLocal, Usuario, get_db

router = APIRouter(tags=["notificacoes"])
_VAPID_CLAIMS = {"sub": "mailto:app@az-eletrica.app"}


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def chaves_vapid(db: Session) -> tuple:
    """(PEM da chave privada, chave pública base64url) — cria na primeira vez."""
    priv = db.get(Config, "vapid_private_pem")
    pub = db.get(Config, "vapid_public")
    if priv and pub:
        return priv.valor, pub.valor
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    k = ec.generate_private_key(ec.SECP256R1())
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()).decode()
    raw = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    db.merge(Config(chave="vapid_private_pem", valor=pem))
    db.merge(Config(chave="vapid_public", valor=_b64url(raw)))
    db.commit()
    return pem, _b64url(raw)


def _enviar_push(assinaturas: List[dict], payload: dict, pem: str):
    try:
        from py_vapid import Vapid
        from pywebpush import WebPushException, webpush
    except Exception:
        return
    vapid = Vapid.from_pem(pem.encode())
    mortas = []
    for a in assinaturas:
        try:
            webpush(subscription_info={"endpoint": a["endpoint"], "keys": {"p256dh": a["p256dh"], "auth": a["auth"]}},
                    data=json.dumps(payload), vapid_private_key=vapid, vapid_claims=dict(_VAPID_CLAIMS), ttl=86400,
                    timeout=10)
        except WebPushException as e:
            if e.response is not None and e.response.status_code in (404, 410):
                mortas.append(a["id"])
        except Exception:
            pass
    if mortas:
        db = SessionLocal()
        try:
            db.query(PushAssinatura).filter(PushAssinatura.id.in_(mortas)).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()


def notificar_admins(db: Session, titulo: str, texto: str, tipo: str = "outro", link: Optional[str] = None):
    """Grava a notificação para cada administrador ativo e dispara o push em segundo plano."""
    admins = [u for u in db.query(Usuario).filter(Usuario.ativo == True).all()  # noqa: E712
              if (u.perfil or "admin") != "diarista"]
    if not admins:
        return
    for u in admins:
        db.add(Notificacao(usuario_id=u.id, tipo=tipo, titulo=titulo, texto=texto, link=link))
    db.commit()
    subs = [{"id": s.id, "endpoint": s.endpoint, "p256dh": s.p256dh, "auth": s.auth}
            for s in db.query(PushAssinatura).filter(PushAssinatura.usuario_id.in_([u.id for u in admins])).all()]
    if subs:
        pem, _ = chaves_vapid(db)
        payload = {"title": titulo, "body": texto, "tag": tipo, "url": "/?tela=" + (link or "dashboard")}
        threading.Thread(target=_enviar_push, args=(subs, payload, pem), daemon=True).start()


# ── Sininho ─────────────────────────────────────────────────────────────────
@router.get("/notificacoes")
def listar(limite: int = 30, db: Session = Depends(get_db), u: Usuario = Depends(exigir_admin)):
    q = db.query(Notificacao).filter(Notificacao.usuario_id == u.id)
    nao_lidas = q.filter(Notificacao.lida == False).count()  # noqa: E712
    itens = q.order_by(Notificacao.id.desc()).limit(max(1, min(limite, 100))).all()
    return {"nao_lidas": nao_lidas,
            "itens": [{"id": n.id, "tipo": n.tipo, "titulo": n.titulo, "texto": n.texto, "link": n.link,
                       "lida": bool(n.lida), "criado_em": n.criado_em.isoformat() + "Z" if n.criado_em else None}
                      for n in itens]}


@router.post("/notificacoes/lidas")
def marcar_lidas(db: Session = Depends(get_db), u: Usuario = Depends(exigir_admin)):
    db.query(Notificacao).filter(Notificacao.usuario_id == u.id, Notificacao.lida == False).update(  # noqa: E712
        {Notificacao.lida: True}, synchronize_session=False)
    db.commit()
    return {"ok": True}


# ── Push no celular ─────────────────────────────────────────────────────────
@router.get("/push/chave")
def chave_publica(db: Session = Depends(get_db), _=Depends(exigir_admin)):
    return {"chave": chaves_vapid(db)[1]}


class AssinaturaSchema(BaseModel):
    endpoint: str
    keys: dict


@router.post("/push/assinar")
def assinar(data: AssinaturaSchema, db: Session = Depends(get_db), u: Usuario = Depends(exigir_admin)):
    p256dh, auth = data.keys.get("p256dh"), data.keys.get("auth")
    if not data.endpoint.startswith("https://") or not p256dh or not auth:
        raise HTTPException(400, "Assinatura inválida")
    s = db.query(PushAssinatura).filter(PushAssinatura.endpoint == data.endpoint).first()
    if s:
        s.usuario_id, s.p256dh, s.auth = u.id, p256dh, auth
    else:
        db.add(PushAssinatura(usuario_id=u.id, endpoint=data.endpoint, p256dh=p256dh, auth=auth))
    db.commit()
    return {"ok": True}


@router.post("/push/cancelar")
def cancelar(data: AssinaturaSchema, db: Session = Depends(get_db), u: Usuario = Depends(exigir_admin)):
    db.query(PushAssinatura).filter(PushAssinatura.endpoint == data.endpoint,
                                    PushAssinatura.usuario_id == u.id).delete(synchronize_session=False)
    db.commit()
    return {"ok": True}


@router.post("/push/teste")
def teste(db: Session = Depends(get_db), u: Usuario = Depends(exigir_admin)):
    subs = [{"id": s.id, "endpoint": s.endpoint, "p256dh": s.p256dh, "auth": s.auth}
            for s in db.query(PushAssinatura).filter(PushAssinatura.usuario_id == u.id).all()]
    if not subs:
        raise HTTPException(400, "Nenhum celular ativado para este usuário")
    pem, _ = chaves_vapid(db)
    threading.Thread(target=_enviar_push, args=(subs, {"title": "AZ Elétrica", "body": "Notificações ativadas neste celular ✅",
                                                         "tag": "teste", "url": "/"}, pem), daemon=True).start()
    return {"ok": True, "celulares": len(subs)}
