from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.models import Base, engine, migrar_colunas
from app.routes import auth, eletricistas, obras, apontamentos, orcamentos, dashboard, financeiro, despesas, ferramentas

# Cria as tabelas
Base.metadata.create_all(bind=engine)
migrar_colunas(engine)

app = FastAPI(
    title="AZ Elétrica — API",
    description="Sistema de gestão de equipes elétricas",
    version="1.0.0"
)

# CORS — permite acesso do PWA e do app mobile
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rotas
app.include_router(auth.router)
app.include_router(eletricistas.router)
app.include_router(obras.router)
app.include_router(apontamentos.router)
app.include_router(orcamentos.router)
app.include_router(dashboard.router)
app.include_router(financeiro.router)
app.include_router(despesas.router)
app.include_router(ferramentas.router)


@app.get("/")
def root():
    return {"status": "AZ Elétrica API rodando", "versao": "1.0.0"}
