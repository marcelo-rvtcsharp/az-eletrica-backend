from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from pydantic import BaseModel
from app.models import Usuario, get_db
from app.auth import hash_senha, verificar_senha, criar_token, get_usuario_atual

router = APIRouter(prefix="/auth", tags=["auth"])


class RegistroSchema(BaseModel):
    nome: str
    email: str
    senha: str


class TokenSchema(BaseModel):
    access_token: str
    token_type: str
    usuario: dict


@router.post("/registro")
def registrar(data: RegistroSchema, db: Session = Depends(get_db)):
    if db.query(Usuario).filter(Usuario.email == data.email).first():
        raise HTTPException(400, "Email já cadastrado")
    usuario = Usuario(
        nome=data.nome,
        email=data.email,
        senha_hash=hash_senha(data.senha),
    )
    db.add(usuario)
    db.commit()
    db.refresh(usuario)
    token = criar_token({"sub": usuario.id})
    return {"access_token": token, "token_type": "bearer", "usuario": {"id": usuario.id, "nome": usuario.nome, "email": usuario.email}}


@router.post("/login")
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    usuario = db.query(Usuario).filter(Usuario.email == form.username).first()
    if not usuario or not verificar_senha(form.password, usuario.senha_hash):
        raise HTTPException(status_code=401, detail="Email ou senha incorretos")
    token = criar_token({"sub": usuario.id})
    return {"access_token": token, "token_type": "bearer", "usuario": {"id": usuario.id, "nome": usuario.nome, "email": usuario.email}}


@router.get("/me")
def me(usuario: Usuario = Depends(get_usuario_atual)):
    return {"id": usuario.id, "nome": usuario.nome, "email": usuario.email, "perfil": usuario.perfil}
