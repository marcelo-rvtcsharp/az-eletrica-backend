from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm, OAuth2PasswordBearer
from typing import Optional
from sqlalchemy.orm import Session
from pydantic import BaseModel
import re
import unicodedata
from typing import List
from app.models import Eletricista, EquipeMembro, Usuario, get_db
from app.auth import hash_senha, verificar_senha, criar_token, get_usuario_atual, exigir_admin

SENHA_PADRAO = "123456"

# Token opcional: permite criar o PRIMEIRO usuário sem login.
# Depois que existe alguém cadastrado, só um usuário logado cria novos logins.
_oauth2_opcional = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

router = APIRouter(prefix="/auth", tags=["auth"])


def _ser(u: Usuario) -> dict:
    return {"id": u.id, "nome": u.nome, "email": u.email, "cargo": u.cargo,
            "perfil": u.perfil or "admin", "ativo": bool(u.ativo),
            "eletricista_id": u.eletricista_id, "senha_padrao": bool(u.senha_padrao),
            "criado_em": u.criado_em.isoformat() if u.criado_em else None}


def _email(e: str) -> str:
    e = (e or "").strip().lower()
    if "@" not in e or "." not in e.split("@")[-1]:
        raise HTTPException(400, "E-mail inválido")
    return e


class RegistroSchema(BaseModel):
    nome: str
    email: str
    senha: str
    cargo: Optional[str] = None


@router.post("/registro")
def registrar(
    data: RegistroSchema,
    db: Session = Depends(get_db),
    token: Optional[str] = Depends(_oauth2_opcional),
):
    if db.query(Usuario).count() > 0:
        if not token:
            raise HTTPException(401, "Faça login para cadastrar novos usuários")
        exigir_admin(get_usuario_atual(token=token, db=db))  # valida o token e exige escritório
    email = _email(data.email)
    if not data.nome.strip():
        raise HTTPException(400, "Informe o nome")
    if len(data.senha) < 6:
        raise HTTPException(400, "A senha precisa ter pelo menos 6 caracteres")
    if db.query(Usuario).filter(Usuario.email.ilike(email)).first():
        raise HTTPException(400, "Email já cadastrado")
    usuario = Usuario(nome=data.nome.strip(), email=email, senha_hash=hash_senha(data.senha),
                      cargo=(data.cargo or "").strip() or None, perfil="admin")
    db.add(usuario)
    db.commit()
    db.refresh(usuario)
    token = criar_token({"sub": usuario.id})
    return {"access_token": token, "token_type": "bearer", "usuario": _ser(usuario)}


@router.post("/login")
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    usuario = db.query(Usuario).filter(Usuario.email.ilike(form.username.strip())).first()
    if not usuario or not verificar_senha(form.password, usuario.senha_hash):
        raise HTTPException(status_code=401, detail="Email ou senha incorretos")
    if not usuario.ativo:
        raise HTTPException(status_code=401, detail="Usuário desativado")
    token = criar_token({"sub": usuario.id})
    return {"access_token": token, "token_type": "bearer", "usuario": _ser(usuario)}


@router.get("/me")
def me(usuario: Usuario = Depends(get_usuario_atual)):
    return _ser(usuario)


# ── Gestão de usuários (todos os usuários têm acesso completo) ─────────────
@router.get("/usuarios")
def listar_usuarios(db: Session = Depends(get_db), _=Depends(exigir_admin)):
    nomes = dict(db.query(Eletricista.id, Eletricista.nome).all())
    equipes = {}
    for m in db.query(EquipeMembro).all():
        equipes.setdefault(m.usuario_id, []).append(m.eletricista_id)
    out = []
    for u in db.query(Usuario).order_by(Usuario.nome).all():
        d = _ser(u)
        d["eletricista_nome"] = nomes.get(u.eletricista_id)
        d["equipe"] = sorted(equipes.get(u.id, []), key=lambda i: nomes.get(i, ""))
        d["equipe_nomes"] = [nomes.get(i, "?") for i in d["equipe"]]
        out.append(d)
    return out


class EquipeSchema(BaseModel):
    eletricista_ids: List[int] = []


@router.put("/usuarios/{id}/equipe")
def definir_equipe(id: int, data: EquipeSchema, db: Session = Depends(get_db), _=Depends(exigir_admin)):
    """Define quem o encarregado (usuário diarista) pode apontar além dele mesmo."""
    u = db.get(Usuario, id)
    if not u or (u.perfil or "admin") != "diarista":
        raise HTTPException(404, "Funcionário com acesso ao app não encontrado")
    ids = {i for i in data.eletricista_ids if i != u.eletricista_id}
    validos = {e.id for e in db.query(Eletricista).filter(Eletricista.id.in_(ids)).all()} if ids else set()
    db.query(EquipeMembro).filter(EquipeMembro.usuario_id == id).delete()
    for i in validos:
        db.add(EquipeMembro(usuario_id=id, eletricista_id=i))
    db.commit()
    return {"ok": True, "equipe": sorted(validos)}


class SenhaSchema(BaseModel):
    atual: str
    nova: str


@router.post("/senha")
def trocar_senha(data: SenhaSchema, db: Session = Depends(get_db), u: Usuario = Depends(get_usuario_atual)):
    """Qualquer usuário troca a própria senha (inclusive diaristas)."""
    if not verificar_senha(data.atual, u.senha_hash):
        raise HTTPException(400, "Senha atual incorreta")
    if len(data.nova) < 6:
        raise HTTPException(400, "A nova senha precisa ter pelo menos 6 caracteres")
    if data.nova == SENHA_PADRAO:
        raise HTTPException(400, "Escolha uma senha diferente de 123456")
    u.senha_hash = hash_senha(data.nova)
    u.senha_padrao = False
    db.commit()
    return _ser(u)


def _sem_acento(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")


def _partes_nome(nome: str) -> list:
    """'ENC.EVERARDO' -> ['everardo']; 'JÚNIOR DE AMONTADA' -> ['junior', 'amontada']"""
    t = _sem_acento(nome or "").lower()
    t = re.sub(r"\b(enc|sr|seu|eng|aux)\.?\s*", " ", t)
    partes = [p for p in re.split(r"[^a-z0-9]+", t) if p and p not in ("de", "da", "do", "dos", "das", "e")]
    return partes or ["funcionario"]


@router.post("/diaristas/gerar")
def gerar_acessos_diaristas(db: Session = Depends(get_db), _=Depends(exigir_admin)):
    """Cria o acesso de cada diarista ativo que ainda não tem: primeironome@gmail.com / 123456."""
    ja_tem = {u.eletricista_id for u in db.query(Usuario).filter(Usuario.eletricista_id.isnot(None)).all()}
    usados = {e.lower() for (e,) in db.query(Usuario.email).all()}
    resultado = []
    for e in (db.query(Eletricista).filter(Eletricista.status == "Ativo", Eletricista.tipo_contrato != "Mensalista")
              .order_by(Eletricista.nome).all()):
        if e.id in ja_tem:
            continue
        partes = _partes_nome(e.nome)
        candidatos = [partes[0]] + (["".join(partes[:2])] if len(partes) > 1 else []) + [f"{partes[0]}{e.id}"]
        email = next(f"{c}@gmail.com" for c in candidatos if f"{c}@gmail.com" not in usados)
        usados.add(email)
        db.add(Usuario(nome=e.nome, email=email, senha_hash=hash_senha(SENHA_PADRAO), perfil="diarista",
                       cargo=e.funcao or "Diarista", eletricista_id=e.id, senha_padrao=True, ativo=True))
        resultado.append({"eletricista_id": e.id, "nome": e.nome, "email": email})
    db.commit()
    return {"criados": resultado, "senha": SENHA_PADRAO}


class UsuarioUpdate(BaseModel):
    nome: str
    email: str
    cargo: Optional[str] = None
    ativo: bool = True
    nova_senha: Optional[str] = None


@router.put("/usuarios/{id}")
def editar_usuario(id: int, data: UsuarioUpdate, db: Session = Depends(get_db),
                   atual: Usuario = Depends(exigir_admin)):
    u = db.get(Usuario, id)
    if not u:
        raise HTTPException(404, "Usuário não encontrado")
    email = _email(data.email)
    if not data.nome.strip():
        raise HTTPException(400, "Informe o nome")
    outro = db.query(Usuario).filter(Usuario.email.ilike(email), Usuario.id != id).first()
    if outro:
        raise HTTPException(400, "Este e-mail já é de outro usuário")
    if u.id == atual.id and not data.ativo:
        raise HTTPException(400, "Você não pode desativar o seu próprio usuário")
    if not data.ativo and (u.perfil or "admin") != "diarista" and db.query(Usuario).filter(
            Usuario.ativo == True, Usuario.id != id, Usuario.perfil != "diarista").count() == 0:  # noqa: E712
        raise HTTPException(400, "Precisa existir pelo menos um usuário ativo")
    if data.nova_senha:
        if len(data.nova_senha) < 6:
            raise HTTPException(400, "A senha precisa ter pelo menos 6 caracteres")
        u.senha_hash = hash_senha(data.nova_senha)
        u.senha_padrao = (data.nova_senha == SENHA_PADRAO)
    u.nome, u.email, u.cargo, u.ativo = data.nome.strip(), email, (data.cargo or "").strip() or None, data.ativo
    db.commit()
    db.refresh(u)
    return _ser(u)
