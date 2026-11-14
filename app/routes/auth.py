from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm, OAuth2PasswordBearer
from typing import Optional
from sqlalchemy.orm import Session
from pydantic import BaseModel
from app.models import Usuario, get_db
from app.auth import hash_senha, verificar_senha, criar_token, get_usuario_atual

# Token opcional: permite criar o PRIMEIRO usuário sem login.
# Depois que existe alguém cadastrado, só um usuário logado cria novos logins.
_oauth2_opcional = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

router = APIRouter(prefix="/auth", tags=["auth"])


def _ser(u: Usuario) -> dict:
    return {"id": u.id, "nome": u.nome, "email": u.email, "cargo": u.cargo,
            "perfil": u.perfil, "ativo": bool(u.ativo),
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
        get_usuario_atual(token=token, db=db)  # valida o token (gera 401 se inválido)
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
def listar_usuarios(db: Session = Depends(get_db), _=Depends(get_usuario_atual)):
    return [_ser(u) for u in db.query(Usuario).order_by(Usuario.nome).all()]


class UsuarioUpdate(BaseModel):
    nome: str
    email: str
    cargo: Optional[str] = None
    ativo: bool = True
    nova_senha: Optional[str] = None


@router.put("/usuarios/{id}")
def editar_usuario(id: int, data: UsuarioUpdate, db: Session = Depends(get_db),
                   atual: Usuario = Depends(get_usuario_atual)):
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
    if not data.ativo and db.query(Usuario).filter(Usuario.ativo == True, Usuario.id != id).count() == 0:  # noqa: E712
        raise HTTPException(400, "Precisa existir pelo menos um usuário ativo")
    if data.nova_senha:
        if len(data.nova_senha) < 6:
            raise HTTPException(400, "A senha precisa ter pelo menos 6 caracteres")
        u.senha_hash = hash_senha(data.nova_senha)
    u.nome, u.email, u.cargo, u.ativo = data.nome.strip(), email, (data.cargo or "").strip() or None, data.ativo
    db.commit()
    db.refresh(u)
    return _ser(u)
