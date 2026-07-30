from sqlalchemy import create_engine, Column, Integer, String, Float, Date, DateTime, ForeignKey, Text, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./az_eletrica.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Usuários ────────────────────────────────────────────────────────────────
class Usuario(Base):
    __tablename__ = "usuarios"
    id          = Column(Integer, primary_key=True, index=True)
    nome        = Column(String, nullable=False)
    email       = Column(String, unique=True, nullable=False)
    senha_hash  = Column(String, nullable=False)
    perfil      = Column(String, default="admin")
    ativo       = Column(Boolean, default=True)
    criado_em   = Column(DateTime, default=datetime.utcnow)


# ── Eletricistas ────────────────────────────────────────────────────────────
class Eletricista(Base):
    __tablename__ = "eletricistas"
    id            = Column(Integer, primary_key=True, index=True)
    nome          = Column(String, nullable=False)
    telefone      = Column(String)
    cpf           = Column(String)
    funcao        = Column(String, default="Eletricista")
    valor_diaria  = Column(Float, nullable=False)
    tipo_contrato = Column(String, default="Diarista")
    nr10_validade = Column(Date, nullable=True)
    nr35_validade = Column(Date, nullable=True)
    data_entrada  = Column(Date, nullable=True)
    status        = Column(String, default="Ativo")
    criado_em     = Column(DateTime, default=datetime.utcnow)
    atualizadoEm  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    apontamentos = relationship("ApontamentoItem", back_populates="eletricista")


# ── Obras ───────────────────────────────────────────────────────────────────
class Obra(Base):
    __tablename__ = "obras"
    id               = Column(Integer, primary_key=True, index=True)
    nome             = Column(String, nullable=False)
    cliente          = Column(String, nullable=False)
    contato          = Column(String)
    cidade           = Column(String)
    bairro           = Column(String)
    endereco         = Column(String)
    tipo_servico     = Column(String, default="Elétrica residencial")
    valor_contratado = Column(Float, nullable=False)
    data_inicio      = Column(Date, nullable=True)
    data_previsao    = Column(Date, nullable=True)
    data_conclusao   = Column(Date, nullable=True)
    status           = Column(String, default="Em andamento")
    alerta_percentual= Column(Integer, default=85)
    observacoes      = Column(Text)
    criado_em        = Column(DateTime, default=datetime.utcnow)
    atualizadoEm     = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    apontamentos = relationship("ApontamentoItem", back_populates="obra")


# ── Apontamentos ─────────────────────────────────────────────────────────────
class Apontamento(Base):
    """Cabeçalho — um por eletricista/dia"""
    __tablename__ = "apontamentos"
    id             = Column(Integer, primary_key=True, index=True)
    eletricista_id = Column(Integer, ForeignKey("eletricistas.id"), nullable=False)
    data           = Column(Date, nullable=False)
    observacao     = Column(Text)
    criado_em      = Column(DateTime, default=datetime.utcnow)
    criado_por     = Column(Integer, ForeignKey("usuarios.id"))

    itens      = relationship("ApontamentoItem",     back_populates="apontamento", cascade="all, delete-orphan")
    adicionais = relationship("ApontamentoAdicional", back_populates="apontamento", cascade="all, delete-orphan")


class ApontamentoItem(Base):
    """Cada obra trabalhada no dia"""
    __tablename__ = "apontamento_itens"
    id             = Column(Integer, primary_key=True, index=True)
    apontamento_id = Column(Integer, ForeignKey("apontamentos.id"), nullable=False)
    eletricista_id = Column(Integer, ForeignKey("eletricistas.id"), nullable=False)
    obra_id        = Column(Integer, ForeignKey("obras.id"), nullable=False)
    valor          = Column(Float, nullable=False)

    apontamento = relationship("Apontamento", back_populates="itens")
    eletricista = relationship("Eletricista", back_populates="apontamentos")
    obra        = relationship("Obra", back_populates="apontamentos")


class ApontamentoAdicional(Base):
    """Adicionais do dia: sábado/feriado, semana pesada, hora extra, outro"""
    __tablename__ = "apontamento_adicionais"
    id             = Column(Integer, primary_key=True, index=True)
    apontamento_id = Column(Integer, ForeignKey("apontamentos.id"), nullable=False)
    eletricista_id = Column(Integer, ForeignKey("eletricistas.id"), nullable=False)
    tipo           = Column(String, nullable=False)
    # sabado_feriado | semana_pesada | hora_extra | outro
    descricao      = Column(String, nullable=False)
    valor          = Column(Float, nullable=False)

    apontamento = relationship("Apontamento", back_populates="adicionais")


# ── Orçamentos ───────────────────────────────────────────────────────────────
class Orcamento(Base):
    __tablename__ = "orcamentos"
    id               = Column(Integer, primary_key=True, index=True)
    numero           = Column(String, unique=True)
    cliente_nome     = Column(String)
    cliente_cnpj     = Column(String)
    cliente_endereco = Column(String)
    obra_descricao   = Column(String)
    data             = Column(Date)
    validade         = Column(Integer, default=10)
    status           = Column(String, default="Rascunho")
    obs              = Column(Text)
    criado_em        = Column(DateTime, default=datetime.utcnow)
    atualizadoEm     = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    itens = relationship("OrcamentoItem", back_populates="orcamento", cascade="all, delete-orphan")


class OrcamentoItem(Base):
    __tablename__ = "orcamento_itens"
    id           = Column(Integer, primary_key=True, index=True)
    orcamento_id = Column(Integer, ForeignKey("orcamentos.id"), nullable=False)
    descricao    = Column(String, nullable=False)
    und          = Column(String, default="UND")
    qtd          = Column(Float, default=0)
    vunit        = Column(Float, default=0)
    ordem        = Column(Integer, default=0)

    orcamento = relationship("Orcamento", back_populates="itens")
